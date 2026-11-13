"""Сканер вилок BC Game (прематч) + Polymarket.

Примеры:
    python scan.py                       # один проход, вилки от 0%
    python scan.py --min-profit 1 --hours 24
    python scan.py --near 15             # показать 15 ближайших к вилке связок
    python scan.py --loop 60             # повторять каждые 60 секунд
"""
from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from dataclasses import dataclass

from arb import bcgame, polymarket
from arb.calc import Arb, bc_complements, find_arb, pm_cost
from arb.match import flip_key, match_events
from arb.model import Event, PmSel

SUSPICIOUS = 0.15      # прибыль выше 15% почти всегда означает ошибку, а не вилку


@dataclass
class Cand:
    bc: Event
    pm: Event
    key: tuple             # исход Polymarket в порядке команд BC
    legs: list             # ключи исходов BC
    bc_inv: float
    sel: PmSel
    arb: Arb | None = None
    edge: float | None = None   # доходность по лучшей цене стакана (может быть < 0)


def label(key: tuple, ev: Event) -> str:
    team = {"c1": ev.c1, "c2": ev.c2, "draw": "ничья"}
    k = key[0]
    if k == "ml":
        return f"победа {team[key[1]]}"
    if k == "1x2":
        return team[key[1]] if key[1] == "draw" else f"победа {team[key[1]]} (осн. время)"
    if k == "not1x2":
        return f"НЕ {team[key[1]]}" if key[1] != "draw" else "НЕ ничья"
    if k == "dc":
        return {"c1x": f"{ev.c1} или ничья", "c12": "не ничья (12)",
                "xc2": f"ничья или {ev.c2}"}[key[1]]
    if k == "total":
        return f"тотал {'больше' if key[2] == 'over' else 'меньше'} {key[1]:g}"
    if k == "hcp":
        line = key[1] if key[2] == "c1" else -key[1]
        return f"фора {team[key[2]]} ({line:+g})"
    return str(key)


def build_candidates(matches) -> list[Cand]:
    out = []
    for bc, pm, flipped, _ in matches:
        for pm_key, sel in pm.sels.items():
            key = flip_key(pm_key) if flipped else pm_key
            best = None
            for legs in bc_complements(key):
                if all(l in bc.odds for l in legs):
                    inv = sum(1.0 / bc.odds[l] for l in legs)
                    if best is None or inv < best[1]:
                        best = (legs, inv)
            if best:
                out.append(Cand(bc, pm, key, best[0], best[1], sel))
    return out


class PmFeed:
    """Список игр Polymarket. Он тяжёлый (мегабайты), а меняется редко, поэтому
    в режиме цикла обновляется в фоновом потоке и не задерживает проходы."""

    def __init__(self, args):
        self.args = args
        self.events: list[Event] = []
        self.updated = 0.0
        self.error = None
        self.bc = bcgame.Feed(args.live)

    def refresh(self):
        self.events = polymarket.fetch_events(self.args.hours, self.args.verbose, self.args.live)
        self.updated = time.time()

    def run_forever(self):
        while True:
            time.sleep(self.args.pm_refresh)
            try:
                self.refresh()
                self.error = None
            except Exception as e:
                self.error = e

    def start_background(self):
        threading.Thread(target=self.run_forever, daemon=True).start()


def scan(args, feed: PmFeed) -> list[Cand]:
    t0 = time.perf_counter()
    bc_events = feed.bc.sync()
    t1 = time.perf_counter()
    pm_events = feed.events
    if not args.live:
        now = time.time()
        bc_events = [e for e in bc_events if now < e.start <= now + args.hours * 3600]
    matches = match_events(bc_events, pm_events)
    cands = build_candidates(matches)

    # В лайве цены из списка игр уже устарели — берём стаканы по всем связкам.
    # В прематче — только там, где по ориентировочной цене связка близка к вилке.
    need = cands if args.live else [
        c for c in cands if c.sel.hint_ask is None
        or c.bc_inv + pm_cost(c.sel.hint_ask, c.sel.fee_rate) < 1.0 + args.slack]
    t2 = time.perf_counter()
    polymarket.load_books(list({id(c.sel): c.sel for c in need}.values()))
    t3 = time.perf_counter()
    for c in need:
        if not c.sel.asks:
            continue
        c.edge = 1.0 / (c.bc_inv + pm_cost(c.sel.asks[0][0], c.sel.fee_rate)) - 1.0
        c.arb = find_arb(c.bc_inv, c.sel.asks, c.sel.fee_rate, args.min_profit / 100.0)

    feed.stats = {"matched": len(matches), "cands": len(cands),
                  "ms": round(1000 * (t3 - t0))}
    age = time.time() - feed.updated
    print(f"[{time.strftime('%H:%M:%S')}] {'ЛАЙВ' if args.live else 'прематч'}: "
          f"BC {len(bc_events)}, PM {len(pm_events)}, совпало {len(matches)}, "
          f"связок {len(cands)}, стаканов {len(need)} | "
          f"BC {1000 * (t1 - t0):.0f} мс, расчёт {1000 * (t2 - t1):.0f} мс, "
          f"стаканы {1000 * (t3 - t2):.0f} мс, всего {1000 * (t3 - t0):.0f} мс"
          + (f" | список PM обновлён {age:.0f} с назад" if age > 5 else "")
          + (f" | ошибка обновления PM: {feed.error}" if feed.error else ""))
    if args.verbose:
        unmatched = {}
        for e in pm_events:
            unmatched[e.sport] = unmatched.get(e.sport, 0) + 1
        for bc, pm, fl, sc in matches:
            unmatched[pm.sport] -= 1
        print("  Polymarket без пары по видам спорта:",
              {k: v for k, v in sorted(unmatched.items(), key=lambda x: -x[1]) if v})
    return need


def show(c: Cand, bank: float) -> None:
    edge = c.arb.profit if c.arb else c.edge
    cost = c.arb.pm_cost if c.arb else pm_cost(c.sel.asks[0][0], c.sel.fee_rate)
    price = c.arb.pm_price if c.arb else c.sel.asks[0][0]
    flag = "  [!] проверьте вручную — слишком большая доходность" if edge > SUSPICIOUS else ""
    start = time.strftime("%d.%m %H:%M", time.localtime(c.bc.start))
    if c.bc.live:
        start = "ЛАЙВ " + " / ".join(x for x in (c.bc.info, c.pm.info) if x)
    print(f"\n{edge * 100:+.2f}%  {c.bc.c1} — {c.bc.c2}  [{c.bc.sport}, {start}]{flag}")
    shares = bank / (c.bc_inv + cost)
    for leg in c.legs:
        k = c.bc.odds[leg]
        print(f"   BC Game    {label(leg, c.bc):<38} кф {k:<6g} ставка ${shares / k:,.2f}")
    print(f"   Polymarket {label(c.key, c.bc):<38} цена {price:.3f} "
          f"(с комиссией {cost:.4f}, кф {1 / cost:.3f})  ${shares * cost:,.2f}")
    if c.arb:
        print(f"   объём по стакану: до ${c.arb.total_stake:,.0f} суммарно, "
              f"прибыль до ${c.arb.profit_usd:,.2f}")
    else:
        print(f"   на лучшей цене доступно {c.sel.asks[0][1]:,.0f} акций")
    print(f"   {c.sel.question}")
    print(f"   {c.bc.url}\n   {c.pm.url}")


def to_dict(c: Cand, bank: float) -> dict:
    """Связка в виде, удобном для страницы в браузере."""
    edge = c.arb.profit if c.arb else c.edge
    cost = c.arb.pm_cost if c.arb else pm_cost(c.sel.asks[0][0], c.sel.fee_rate)
    price = c.arb.pm_price if c.arb else c.sel.asks[0][0]
    shares = bank / (c.bc_inv + cost)
    when = time.strftime("%d.%m %H:%M", time.localtime(c.bc.start))
    if c.bc.live:
        when = "лайв " + " / ".join(x for x in (c.bc.info, c.pm.info) if x)
    legs = [{"site": "BC Game", "what": label(l, c.bc), "price": f"кф {c.bc.odds[l]:g}",
             "stake": shares / c.bc.odds[l]} for l in c.legs]
    legs.append({"site": "Polymarket", "what": label(c.key, c.bc),
                 "price": f"цена {price:.3f} (кф {1 / cost:.3f} с комиссией)",
                 "stake": shares * cost})
    volume = (f"Объём по стакану: до ${c.arb.total_stake:,.0f}, прибыль до ${c.arb.profit_usd:,.2f}"
              if c.arb else f"На лучшей цене доступно {c.sel.asks[0][1]:,.0f} акций")
    return {"id": f"{c.bc.id}|{c.key}|{c.legs}", "pct": round(edge * 100, 2),
            "match": f"{c.bc.c1} — {c.bc.c2}", "sport": c.bc.sport, "when": when,
            "suspicious": edge > SUSPICIOUS, "legs": legs, "volume": volume,
            "bc_url": c.bc.url, "pm_url": c.pm.url}


def beep() -> None:
    """Звуковой сигнал на этом компьютере, не блокирует сканер."""
    def run():
        try:
            import winsound
            for freq in (880, 1175, 1568):
                winsound.Beep(freq, 110)
        except Exception:
            print("\a", end="", flush=True)
    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(description="Сканер вилок BC Game + Polymarket")
    p.add_argument("--hours", type=float, default=48, help="горизонт по времени начала матчей")
    p.add_argument("--min-profit", type=float, default=0.0, help="минимальная доходность, %%")
    p.add_argument("--bank", type=float, default=100.0, help="сумма для расчёта ставок, $")
    p.add_argument("--near", type=int, default=0,
                   help="дополнительно показать N связок, ближайших к вилке")
    p.add_argument("--slack", type=float, default=0.05,
                   help="запас при отборе стаканов (0.05 = смотреть связки до -5%%)")
    p.add_argument("--live", action="store_true", help="сканировать идущие матчи вместо прематча")
    p.add_argument("--loop", type=float, default=None,
                   help="повторять каждые N секунд (0 — без пауз, сразу следующий проход)")
    p.add_argument("--pm-refresh", type=float, default=None,
                   help="как часто обновлять список игр Polymarket в цикле, с "
                        "(по умолчанию 30 в лайве, 120 в прематче)")
    p.add_argument("--passes", type=int, default=0, help="остановиться после N проходов")
    p.add_argument("--csv", help="дописывать найденные вилки в CSV-файл")
    p.add_argument("--sound", action="store_true", help="звуковой сигнал при новой вилке")
    p.add_argument("--web", type=int, nargs="?", const=8765, default=None, metavar="PORT",
                   help="страница с вилками и уведомлениями в браузере (порт, по умолчанию 8765)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    looping = args.loop is not None
    if args.pm_refresh is None:
        args.pm_refresh = 30 if args.live else 120

    hub = None
    if args.web is not None:
        from arb import web
        hub = web.serve(args.web)
        print(f"Страница с вилками: http://localhost:{args.web}  "
              f"(нажмите на ней «Включить уведомления и звук»)")
    alerted: dict = {}          # вилка -> когда последний раз была на экране

    feed = PmFeed(args)
    feed.refresh()
    if looping:
        feed.start_background()

    passes = 0
    while True:
        started = time.perf_counter()
        passes += 1
        try:
            checked = scan(args, feed)
        except Exception as e:            # сеть нестабильна — в цикле не падаем
            if not looping:
                raise
            print(f"ошибка прохода: {e}")
            feed.bc.version = None        # после сбоя берём линию BC заново
            time.sleep(max(args.loop, 1))
            continue
        arbs = sorted((c for c in checked if c.arb), key=lambda c: -c.arb.profit)
        print(f"Вилок от {args.min_profit:g}%: {len(arbs)}")
        for c in arbs:
            show(c, args.bank)
        # Сигнал — только на новую вилку (или вернувшуюся после минуты отсутствия).
        now = time.time()
        is_new = False
        for c in arbs:
            key = (c.bc.id, c.key, tuple(c.legs))
            if now - alerted.get(key, 0) > 60:
                is_new = True
            alerted[key] = now
        if is_new and args.sound:
            beep()
        rest = []
        if args.near:
            rest = sorted((c for c in checked if not c.arb and c.edge is not None),
                          key=lambda c: -c.edge)[:args.near]
        if hub:
            hub.publish({"time": time.strftime("%H:%M:%S"), "mode": "лайв" if args.live else "прематч",
                         "min_profit": args.min_profit, **feed.stats,
                         "arbs": [to_dict(c, args.bank) for c in arbs],
                         "near": [to_dict(c, args.bank) for c in rest]})
        if args.near:
            print(f"\n--- Ближайшие к вилке ({len(rest)}) ---")
            for c in rest:
                show(c, args.bank)
        if args.csv and arbs:
            with open(args.csv, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                for c in arbs:
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), c.bc.sport, c.bc.c1, c.bc.c2,
                                time.strftime("%Y-%m-%d %H:%M", time.localtime(c.bc.start)),
                                " + ".join(label(l, c.bc) for l in c.legs),
                                " / ".join(f"{c.bc.odds[l]:g}" for l in c.legs),
                                label(c.key, c.bc), f"{c.arb.pm_price:.4f}",
                                f"{c.arb.profit * 100:.2f}", f"{c.arb.total_stake:.0f}",
                                c.bc.url, c.pm.url])
        if not looping or (args.passes and passes >= args.passes):
            break
        time.sleep(max(0.0, args.loop - (time.perf_counter() - started)))


if __name__ == "__main__":
    main()
