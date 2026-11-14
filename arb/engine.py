"""Движок сканера: держит обе площадки в памяти и пересчитывает связки по событию.

Никакого цикла «опросить всё — посчитать всё» здесь нет. Три источника сами
сообщают об изменениях:

* поток линии BC Game (long-poll, отдельный поток на прематч и на лайв);
* поток стаканов Polymarket (WebSocket);
* список игр Polymarket (тяжёлый, обновляется в фоне раз в минуту).

Когда меняется коэффициент или стакан, пересчитываются только связки, в которых
он участвует, прямо в потоке, получившем данные. От прихода данных до готовой
вилки проходят доли миллисекунды; всё остальное время — это задержка самих площадок.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import bcgame, polymarket, view
from .books import Books
from .calc import Arb, bc_complements, find_arb, pm_cost
from .http import POOL
from .match import WEAK_SCORE, flip_key, match_events
from .model import Event, PmSel

HIDDEN_FILE = "hidden.json"    # что скрыто кнопками на странице
BETS_FILE = "bets.json"         # сохранённые ставки (учёт)
HIDDEN_KEEP = 3 * 86400         # скрытые матчи забываются через три дня
GRACE = 2.0             # вилка, пропавшая меньше чем на столько секунд, считается той же
PREMATCH_PAST = 900     # сколько секунд после времени начала ещё верить прематч-линии


@dataclass(eq=False)
class Cand:
    """Связка: токен Polymarket против дополняющих исходов BC Game."""
    bc_id: str
    pm: Event
    key: tuple                  # исход Polymarket в порядке команд BC
    sel: PmSel
    weak: bool                  # матч сопоставлен по одной команде
    options: list               # варианты плеча BC (наборы ключей исходов)
    bc: Event | None = None
    legs: list | None = None    # выбранное плечо BC
    bc_inv: float = 0.0
    arb: Arb | None = None
    edge: float | None = None   # доходность по лучшей цене стакана (может быть < 0)
    episode: dict | None = None  # текущая «жизнь» вилки: start, peak, shown, gone_at


class Engine:
    def __init__(self, args):
        self.args = args
        self.min_profit = args.min_profit / 100.0
        self.max_profit = args.max_profit / 100.0 if args.max_profit else None
        self.hold = max(args.hold, 0.0) if args.watch else 0.0
        # значение -> (подпись, когда). "pin" — закреплённые связки: они видны на
        # странице, даже когда вилки по ним нет.
        self.hidden = {"match": {}, "sport": {}, "market": {}, "pin": {}}
        self._load_hidden()
        self.pin_last: dict = {}        # последняя известная карточка закреплённой связки
        self.bets: list = []
        self._load_bets()
        self.sports: dict = {}          # вид спорта -> сколько матчей сопоставлено
        self.lock = threading.RLock()
        modes = {"all": (False, True), "live": (True,), "prematch": (False,)}[args.mode]
        self.feeds = [bcgame.Feed(live, self._bc_changed) for live in modes]
        self.pm_live = {"all": None, "live": True, "prematch": False}[args.mode]
        self.bc_by_mode: dict[str, dict] = {f.mode: {} for f in self.feeds}
        self.pm_events: list[Event] = []
        self.pm_updated = 0.0
        self.pm_error = None
        self.books = Books(self._books_changed)
        self.cands: dict[tuple, Cand] = {}
        self.by_bc: dict[str, list] = {}
        self.by_tok: dict[str, list] = {}
        self.arbs: dict[tuple, Cand] = {}
        self.matched = 0
        self.unmatched: dict = {}
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.wake = threading.Event()           # есть что показать
        self._rematch = threading.Event()
        self.watching = False
        self.evals = 0
        self.eval_ms = 0.0                      # сглаженное время одного пересчёта
        self.bc_lag: dict[str, float] = {}      # сглаженное отставание линии BC, с

    # ------------------------------------------------------------------ запуск
    def start(self, watch: bool) -> None:
        """Первое заполнение; при watch=True дальше работают фоновые потоки."""
        errors = []

        def boot(feed):
            try:
                feed.step()
                if watch:
                    feed.catch_up()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=boot, args=(f,), daemon=True) for f in self.feeds]
        for t in threads:
            t.start()
        self._refresh_pm()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        with self.lock:
            for f in self.feeds:
                self.bc_by_mode[f.mode] = {e.id: e for e in f.events()}
        self.watching = watch
        self.rematch()
        if watch:
            for f in self.feeds:
                threading.Thread(target=f.run, daemon=True).start()
            threading.Thread(target=self._pm_loop, daemon=True).start()
            threading.Thread(target=self._rematch_loop, daemon=True).start()

    # ------------------------------------------------------------- Polymarket
    def _refresh_pm(self) -> None:
        raw = polymarket.fetch_raw(self.args.hours, self.pm_live, self.args.verbose)
        events = polymarket.parse_events(raw, self.args.hours, self.pm_live)
        with self.lock:
            self.pm_events = events
            self.pm_updated = time.time()

    def _pm_loop(self) -> None:
        while True:
            time.sleep(self.args.pm_refresh)
            try:
                self._refresh_pm()
                self.pm_error = None
                self._rematch.set()
            except Exception as e:
                self.pm_error = e

    # ---------------------------------------------------------- сопоставление
    def _bc(self, eid: str) -> Event | None:
        for mode in ("live", "prematch"):       # идущий матч важнее прематч-записи
            d = self.bc_by_mode.get(mode)
            if d:
                ev = d.get(eid)
                if ev is not None:
                    return ev
        return None

    def _bc_events(self) -> list[Event]:
        now = time.time()
        horizon = now + self.args.hours * 3600
        out = dict(self.bc_by_mode.get("prematch") or {})
        out = {i: e for i, e in out.items() if now - PREMATCH_PAST < e.start <= horizon}
        out.update(self.bc_by_mode.get("live") or {})
        return list(out.values())

    def _rematch_loop(self) -> None:
        while True:
            self._rematch.wait()
            self._rematch.clear()
            try:
                self.rematch()
            except Exception as e:
                self.pm_error = e
            time.sleep(1.0)                     # не чаще раза в секунду

    def rematch(self) -> None:
        """Заново сопоставить события и собрать связки. Уже известные связки
        сохраняются, чтобы «жизнь» идущих вилок не прерывалась."""
        with self.lock:
            bc_events, pm_events = self._bc_events(), list(self.pm_events)
        matches = match_events(bc_events, pm_events)
        cands: dict[tuple, Cand] = {}
        for bc, pm, flipped, score in matches:
            for pm_key, sel in pm.sels.items():
                key = flip_key(pm_key) if flipped else pm_key
                options = bc_complements(key)
                if not options:
                    continue
                ident = (bc.id, sel.token)
                c = self.cands.get(ident)
                if c is None:
                    c = Cand(bc.id, pm, key, sel, score <= WEAK_SCORE, options)
                else:
                    c.pm, c.key, c.sel, c.options = pm, key, sel, options
                    c.weak = score <= WEAK_SCORE
                cands[ident] = c
        by_bc: dict[str, list] = {}
        by_tok: dict[str, list] = {}
        for c in cands.values():
            by_bc.setdefault(c.bc_id, []).append(c)
            by_tok.setdefault(c.sel.token, []).append(c)
        unmatched: dict = {}
        used = {pm.id for _, pm, _, _ in matches}
        for e in pm_events:
            if e.id not in used:
                unmatched[e.sport] = unmatched.get(e.sport, 0) + 1
        with self.lock:
            for ident, c in self.cands.items():     # связки, которых больше нет
                if ident not in cands:
                    self._track(c, None)
                    if c.episode:               # reap() эту связку уже не увидит
                        self.events.put(("gone", c.episode))
                        c.episode = None
            self.cands, self.by_bc, self.by_tok = cands, by_bc, by_tok
            self.matched, self.unmatched = len(matches), unmatched
            sports: dict = {}
            for bc, _, _, _ in matches:
                sports[bc.sport] = sports.get(bc.sport, 0) + 1
            self.sports = sports
        new = [t for t in by_tok if not self.books.known(t)]
        if new:
            self.books.load(new)
        if self.watching:
            self.books.subscribe(by_tok)
        with self.lock:
            for c in cands.values():
                self._eval(c)

    # ---------------------------------------------------------------- расчёт
    def _eval(self, c: Cand) -> None:
        """Пересчитать одну связку. Вызывается под self.lock."""
        bc = c.bc = self._bc(c.bc_id)
        best = None
        if bc is not None:
            odds = bc.odds
            for legs in c.options:
                inv = 0.0
                for leg in legs:
                    k = odds.get(leg)
                    if k is None:
                        break
                    inv += 1.0 / k
                else:
                    if best is None or inv < best[1]:
                        best = (legs, inv)
        asks = self.books.asks(c.sel.token)
        arb = None
        h = self.hidden
        hidden = bool(h["match"] or h["sport"] or h["market"]) and (
            c.bc_id in h["match"] or c.pm.sport in h["sport"]
            or f"{c.bc_id}|{c.sel.token}" in h["market"])
        if best is None or not asks:
            c.legs, c.edge = None, None
        else:
            c.legs, c.bc_inv = best
            fee = c.sel.fee_rate
            c.edge = 1.0 / (c.bc_inv + pm_cost(asks[0][0], fee)) - 1.0
            if c.edge >= self.min_profit and not hidden:
                arb = find_arb(c.bc_inv, asks, fee, self.min_profit)
                if arb and (arb.total_stake < self.args.min_volume or
                            (self.max_profit is not None and arb.profit > self.max_profit)):
                    arb = None
        self.evals += 1
        self._track(c, arb)

    def _track(self, c: Cand, arb: Arb | None) -> None:
        c.arb = arb
        ident = (c.bc_id, c.sel.token)
        ep = c.episode
        if arb:
            self.arbs[ident] = c
            if ep is None:
                # announced — о вилке уже сообщили (сразу или когда продержалась --hold секунд)
                ep = c.episode = {"start": time.time(), "peak": arb.profit, "gone_at": None,
                                  "snap": None, "cand": c, "announced": self.hold <= 0}
                ep["snap"] = view.to_dict(c, self.args.bank, self.books.asks(c.sel.token))
                if ep["announced"]:
                    self.events.put(("new", ep))
                if self.watching:               # контрольная сверка стакана по HTTP
                    POOL.submit(self._verify, c.sel.token)
            else:
                ep["gone_at"] = None
                if arb.profit > ep["peak"]:
                    ep["peak"] = arb.profit
            self.wake.set()
        elif ident in self.arbs:
            del self.arbs[ident]
            if ep is not None:
                ep["gone_at"] = time.time()
            self.wake.set()

    def _verify(self, token: str) -> None:
        try:
            changed = self.books.load([token], timeout=5)
        except Exception:
            return
        if changed:
            self._books_changed(changed)

    def reap(self) -> None:
        """Сообщить о вилках, продержавшихся --hold секунд, и закрыть «жизни»
        вилок, которых нет дольше GRACE секунд."""
        now = time.time()
        with self.lock:
            cands = list(self.cands.values())
        for c in cands:
            ep = c.episode
            if ep and not ep["announced"] and not ep["gone_at"] and now - ep["start"] >= self.hold:
                with self.lock:
                    if c.episode is ep and c.arb and not ep["gone_at"]:
                        ep["announced"] = True
                        ep["snap"] = view.to_dict(c, self.args.bank,
                                                  self.books.asks(c.sel.token)) or ep["snap"]
                        self.events.put(("new", ep))
            if ep and ep["gone_at"] and now - ep["gone_at"] > GRACE:
                with self.lock:
                    if c.episode is ep and ep["gone_at"]:
                        c.episode = None
                        self.events.put(("gone", ep))

    def flush(self) -> list:
        """Все ещё открытые «жизни» вилок — для записи при остановке."""
        with self.lock:
            return [c.episode for c in self.cands.values() if c.episode]

    # ------------------------------------------------ обработчики изменений
    def _bc_changed(self, feed, changed: dict) -> None:
        t0 = time.perf_counter()
        if feed.generated:
            lag = feed.received - feed.generated
            old = self.bc_lag.get(feed.mode)
            self.bc_lag[feed.mode] = lag if old is None else old + 0.2 * (lag - old)
        with self.lock:
            d = self.bc_by_mode[feed.mode]
            structural = False
            n = 0
            for eid, ev in changed.items():
                if ev is None:
                    d.pop(eid, None)
                else:
                    if eid not in d:
                        structural = True       # новое событие — нужно сопоставление
                    d[eid] = ev
                for c in self.by_bc.get(eid, ()):
                    self._eval(c)
                    n += 1
            if structural:
                self._rematch.set()
        if n:
            self._timed(t0)

    def _books_changed(self, tokens: set) -> None:
        t0 = time.perf_counter()
        n = 0
        with self.lock:
            for tok in tokens:
                for c in self.by_tok.get(tok, ()):
                    self._eval(c)
                    n += 1
        if n:
            self._timed(t0)

    def _timed(self, t0: float) -> None:
        ms = (time.perf_counter() - t0) * 1000
        self.eval_ms += 0.05 * (ms - self.eval_ms)

    # ------------------------------------------------------------- для вывода
    def snapshot(self, near: int = 0) -> dict:
        with self.lock:
            bank = self.args.bank
            # сверху — вилки, которые уже продержались; среди них сначала прематч
            # (живёт минутами), потом лайв; внутри группы — по доходности
            def order(c):
                ep = c.episode
                return (not (ep and ep["announced"]), bool(c.bc and c.bc.live), -c.arb.profit)
            arbs = sorted(self.arbs.values(), key=order)
            arbs = [view.to_dict(c, bank, self.books.asks(c.sel.token)) for c in arbs]
            shown = {a["id"] for a in arbs if a}
            pinned = []
            for ident in self.hidden["pin"]:
                if ident in shown:
                    continue
                bc_id, _, token = ident.partition("|")
                c = self.cands.get((bc_id, token))
                d = view.to_dict(c, bank, self.books.asks(token)) if c else None
                if d:
                    self.pin_last[ident] = d
                else:                   # матч закончился или рынок закрыт
                    d = self.pin_last.get(ident)
                    if d:
                        d = {**d, "stale": True}
                if d:
                    pinned.append(d)
            rest = []
            if near:
                pool = sorted((c for c in self.cands.values() if not c.arb and c.edge is not None),
                              key=lambda c: -c.edge)[:near]
                rest = [view.to_dict(c, bank, self.books.asks(c.sel.token)) for c in pool]
            hidden = {k: [{"value": v, "label": lab} for v, (lab, _) in d.items()]
                      for k, d in self.hidden.items()}
            del hidden["pin"]
            return {"arbs": [a for a in arbs if a], "near": [a for a in rest if a],
                    "pinned": pinned, "pins": list(self.hidden["pin"]),
                    "bets": list(self.bets), "sports": dict(self.sports),
                    "hold": self.hold, "hidden": hidden, **self.stats()}

    # ------------------------------------------------------------ учёт ставок
    def _load_bets(self) -> None:
        try:
            with open(BETS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.bets = data
        except (OSError, ValueError):
            pass

    def _save_bets(self) -> None:
        try:
            tmp = BETS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.bets, f, ensure_ascii=False, indent=1)
            os.replace(tmp, BETS_FILE)
        except OSError:
            pass

    def add_bet(self, body: dict) -> None:
        """Сохранить проставленную вилку. Суммы приходят со страницы — их вводит человек."""
        def num(x):
            try:
                v = float(x)
            except (TypeError, ValueError):
                return 0.0
            return v if 0 <= v < 1e9 else 0.0

        legs = []
        for leg in (body.get("legs") or [])[:4]:
            if isinstance(leg, dict):
                legs.append({"site": str(leg.get("site", ""))[:20], "what": str(leg.get("what", ""))[:120],
                             "stake": num(leg.get("stake")), "k": num(leg.get("k")),
                             "payout": num(leg.get("payout"))})
        if not legs:
            return
        total = sum(l["stake"] for l in legs)
        bet = {"id": f"{time.time():.3f}", "ts": time.time(),
               "match": str(body.get("match", ""))[:160], "sport": str(body.get("sport", ""))[:30],
               "when": str(body.get("when", ""))[:60], "note": str(body.get("note", ""))[:200],
               "bc_url": str(body.get("bc_url", ""))[:400], "pm_url": str(body.get("pm_url", ""))[:400],
               "legs": legs, "total": total,
               # при каком исходе сколько вернётся; худший случай — гарантированный результат
               "profit_min": min(l["payout"] for l in legs) - total,
               "profit_max": max(l["payout"] for l in legs) - total}
        with self.lock:
            self.bets.append(bet)
            self._save_bets()
        self.wake.set()

    def delete_bet(self, bet_id: str) -> None:
        with self.lock:
            self.bets = [b for b in self.bets if b.get("id") != bet_id]
            self._save_bets()
        self.wake.set()

    # ------------------------------------------------------- скрытое вручную
    def _load_hidden(self) -> None:
        try:
            with open(HIDDEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        for kind in self.hidden:
            for value, item in (data.get(kind) or {}).items():
                if kind == "sport" or now - item[1] < HIDDEN_KEEP:
                    self.hidden[kind][value] = (item[0], item[1])

    def _save_hidden(self) -> None:
        try:
            tmp = HIDDEN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.hidden, f, ensure_ascii=False)
            os.replace(tmp, HIDDEN_FILE)
        except OSError:
            pass

    def hide(self, kind: str, value: str, label: str = "") -> None:
        """Скрыть матч, вид спорта или одну связку: вилки по ним не показываются."""
        if kind not in self.hidden or not value:
            return
        with self.lock:
            self.hidden[kind][str(value)] = (str(label)[:120], time.time())
            self._save_hidden()
            for c in self.cands.values():
                self._eval(c)
        self.wake.set()

    def unhide(self, kind: str | None = None, value: str | None = None) -> None:
        with self.lock:
            for k, d in self.hidden.items():
                if kind == k or (kind is None and k != "pin"):   # «Показать всё» не снимает закрепление
                    if value is None:
                        d.clear()
                    else:
                        d.pop(str(value), None)
            self._save_hidden()
            for c in self.cands.values():
                self._eval(c)
        self.wake.set()

    def stats(self) -> dict:
        now = time.time()
        bc = {}
        for f in self.feeds:
            bc[f.mode] = {"events": len(self.bc_by_mode[f.mode]),
                          "lag": self.bc_lag.get(f.mode),
                          "age": now - f.received if f.received else None,
                          "error": str(f.error) if f.error else None}
        b = self.books
        return {"matched": self.matched, "cands": len(self.cands), "pm_events": len(self.pm_events),
                "pm_age": now - self.pm_updated, "pm_error": str(self.pm_error) if self.pm_error else None,
                "bc": bc, "transport": bcgame.transport(),
                "ws": {"on": b.streaming, "alive": b.alive(), "conns": len(b.conns),
                       "tokens": len(b.subscribed), "lag_ms": b.lag_ms,
                       "idle": now - b.last_msg if b.last_msg else None,
                       "messages": b.messages},
                "eval_ms": self.eval_ms, "evals": self.evals, "unmatched": dict(self.unmatched)}
