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
        if best is None or not asks:
            c.legs, c.edge = None, None
        else:
            c.legs, c.bc_inv = best
            fee = c.sel.fee_rate
            c.edge = 1.0 / (c.bc_inv + pm_cost(asks[0][0], fee)) - 1.0
            if c.edge >= self.min_profit:
                arb = find_arb(c.bc_inv, asks, fee, self.min_profit)
                if arb and arb.total_stake < self.args.min_volume:
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
                snap = view.to_dict(c, self.args.bank, self.books.asks(c.sel.token))
                c.episode = {"start": time.time(), "peak": arb.profit, "gone_at": None,
                             "snap": snap, "cand": c}
                self.events.put(("new", c.episode))
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
        """Закрыть «жизни» вилок, которых нет дольше GRACE секунд."""
        now = time.time()
        with self.lock:
            cands = list(self.cands.values())
        for c in cands:
            ep = c.episode
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
            arbs = sorted(self.arbs.values(), key=lambda c: -c.arb.profit)
            arbs = [view.to_dict(c, bank, self.books.asks(c.sel.token)) for c in arbs]
            rest = []
            if near:
                pool = sorted((c for c in self.cands.values() if not c.arb and c.edge is not None),
                              key=lambda c: -c.edge)[:near]
                rest = [view.to_dict(c, bank, self.books.asks(c.sel.token)) for c in pool]
            return {"arbs": [a for a in arbs if a], "near": [a for a in rest if a],
                    **self.stats()}

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
