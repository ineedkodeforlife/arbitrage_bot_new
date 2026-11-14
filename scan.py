"""Сканер вилок BC Game + Polymarket (прематч и лайв, включая киберспорт).

Примеры:
    python scan.py                        # один снимок: вилки от 0%
    python scan.py --near 15              # и 15 ближайших к вилке связок
    python scan.py --watch --sound --web  # непрерывно: поток обеих площадок
    python scan.py --watch --mode live --min-profit 1 --csv arbs.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
import time

from arb import http, view
from arb.engine import Engine


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


def write_csv(path: str, rows: list) -> None:
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if fresh:
            w.writerow(view.CSV_HEAD)
        w.writerows(rows)


def status_line(s: dict) -> str:
    parts = [f"[{time.strftime('%H:%M:%S')}] совпало матчей {s['matched']}, связок {s['cands']}"]
    for mode, b in s["bc"].items():
        name = "лайв" if mode == "live" else "прематч"
        if b["error"]:
            parts.append(f"BC {name}: ошибка ({b['error'][:60]})")
        elif b["lag"] is not None:
            parts.append(f"BC {name}: {b['events']} соб., отставание {b['lag']:.1f} с")
        else:
            parts.append(f"BC {name}: {b['events']} соб.")
    ws = s["ws"]
    if ws["on"]:
        state = "поток" if ws["alive"] else "поток ПЕРЕПОДКЛЮЧАЕТСЯ"
        parts.append(f"PM: {s['pm_events']} соб., {state} {ws['tokens']} стаканов, "
                     f"задержка {max(ws['lag_ms'], 0):.0f} мс")
    else:
        parts.append(f"PM: {s['pm_events']} соб.")
    if s["pm_age"] > 150:
        parts.append(f"список PM обновлён {s['pm_age']:.0f} с назад")
    if s["pm_error"]:
        parts.append(f"ошибка PM: {s['pm_error'][:80]}")
    if s["evals"]:
        parts.append(f"пересчёт {s['eval_ms']:.2f} мс")
    return " | ".join(parts)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(description="Сканер вилок BC Game + Polymarket")
    p.add_argument("--watch", action="store_true",
                   help="работать непрерывно: вилка показывается, как только появилась")
    p.add_argument("--mode", choices=("all", "prematch", "live"), default="all",
                   help="какие матчи смотреть (по умолчанию и прематч, и лайв)")
    p.add_argument("--live", action="store_true", help="то же, что --mode live")
    p.add_argument("--hours", type=float, default=48, help="горизонт по времени начала матчей")
    p.add_argument("--min-profit", type=float, default=0.0, help="минимальная доходность, %%")
    p.add_argument("--min-volume", type=float, default=0.0,
                   help="не показывать вилки, в которые помещается меньше N долларов")
    p.add_argument("--max-profit", type=float, default=0.0,
                   help="не показывать вилки с доходностью выше N %% (обычно это ошибка); 0 — показывать все")
    p.add_argument("--hold", type=float, default=3.0,
                   help="в режиме --watch сообщать о вилке, когда она продержалась N секунд "
                        "(0 — сразу); секундные всплески в лайве руками всё равно не поймать")
    p.add_argument("--bank", type=float, default=100.0, help="сумма для расчёта ставок, $")
    p.add_argument("--near", type=int, default=0,
                   help="дополнительно показать N связок, ближайших к вилке")
    p.add_argument("--pm-refresh", type=float, default=60,
                   help="как часто обновлять список игр Polymarket, с")
    p.add_argument("--status", type=float, default=15, help="как часто печатать строку состояния, с")
    p.add_argument("--duration", type=float, default=0, help="остановиться через N секунд")
    p.add_argument("--csv", help="записывать вилки в CSV-файл (с временем жизни каждой)")
    p.add_argument("--sound", action="store_true", help="звуковой сигнал при новой вилке")
    p.add_argument("--web", type=int, nargs="?", const=8765, default=None, metavar="PORT",
                   help="страница с вилками в браузере; в режиме --watch включена сама "
                        "(порт по умолчанию 8765)")
    p.add_argument("--no-web", action="store_true", help="не запускать страницу, только консоль")
    p.add_argument("--no-browser", action="store_true",
                   help="не открывать страницу в браузере автоматически")
    p.add_argument("--tg-token", default=os.environ.get("ARB_TG_TOKEN"),
                   help="токен бота Telegram для уведомлений (или переменная ARB_TG_TOKEN)")
    p.add_argument("--tg-chat", default=os.environ.get("ARB_TG_CHAT"),
                   help="chat_id, куда слать уведомления (или переменная ARB_TG_CHAT)")
    p.add_argument("--proxy", help="HTTP-прокси для линии BC Game: host:port или none "
                                   "(по умолчанию берётся из HTTPS_PROXY / настроек системы)")
    p.add_argument("--verbose", action="store_true")
    # старые ключи: --loop N включал цикл, --slack отбирал стаканы
    p.add_argument("--loop", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--slack", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--passes", type=int, default=0, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.live:
        args.mode = "live"
    if args.loop is not None:
        args.watch = True
    http.set_proxy(args.proxy)

    hub = None
    if args.watch and args.web is None and not args.no_web:
        args.web = 8765
    if args.web is not None and not args.no_web:
        from arb import web
        url = f"http://localhost:{args.web}"
        try:
            hub = web.serve(args.web)
        except OSError as e:
            print(f"Страница не запущена: порт {args.web} занят ({e}). Возможно, сканер уже "
                  f"работает в другом окне; другой порт задаётся так: --web 8766")
        else:
            print(f"Страница с вилками: {url}  (нажмите на ней «Включить уведомления и звук»)")
            if not args.no_browser:
                import webbrowser
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    tg = None
    if args.tg_token or args.tg_chat:
        if not (args.tg_token and args.tg_chat):
            sys.exit("Для Telegram нужны и токен, и chat_id (--tg-token и --tg-chat).")
        from arb.notify import Telegram
        tg = Telegram(args.tg_token, args.tg_chat)
        err = tg.check()
        if err:
            print(f"Telegram: пробное сообщение не отправилось ({err[:200]}). "
                  f"Уведомления в Telegram отключены.")
            tg = None
        else:
            print("Telegram: пробное сообщение отправлено.")

    engine = Engine(args)
    if hub:
        def command(path, body):
            if path == "/hide":
                engine.hide(str(body.get("kind")), str(body.get("value") or ""),
                            str(body.get("label") or ""))
            else:
                engine.unhide(body.get("kind"), body.get("value"))
        hub.on_command = command
    t0 = time.perf_counter()
    print("Загружаю линию BC Game и список игр Polymarket…")
    engine.start(args.watch)
    s = engine.stats()
    print(f"Готово за {time.perf_counter() - t0:.1f} с (линия BC Game: "
          f"{ {'proxy': 'постоянное соединение через прокси', 'direct': 'постоянное соединение', 'curl': 'curl'}.get(s['transport'], s['transport']) }).")
    print(status_line(s))
    if args.verbose:
        print("  Polymarket без пары по видам спорта:",
              dict(sorted(s["unmatched"].items(), key=lambda x: -x[1])))

    mode_name = {"all": "прематч + лайв", "live": "лайв", "prematch": "прематч"}[args.mode]

    def publish():
        if hub:
            snap = engine.snapshot(args.near)
            hub.publish({"time": time.strftime("%H:%M:%S"), "mode": mode_name,
                         "min_profit": args.min_profit, "watch": args.watch, **snap})

    if not args.watch:
        snap = engine.snapshot(args.near)
        print(f"Вилок от {args.min_profit:g}%: {len(snap['arbs'])}")
        for d in snap["arbs"]:
            print("\n" + view.text(d))
        if args.near:
            print(f"\n--- Ближайшие к вилке ({len(snap['near'])}) ---")
            for d in snap["near"]:
                print("\n" + view.text(d))
        if args.csv and snap["arbs"]:
            now = time.time()
            write_csv(args.csv, [view.csv_row(d, now, None, d["pct"] / 100) for d in snap["arbs"]])
        publish()
        return

    print(f"Слежу за вилками от {args.min_profit:g}% ({mode_name})"
          + (f", сообщаю о тех, что держатся от {args.hold:g} с" if args.hold > 0 else "")
          + ". Остановить: Ctrl+C.")
    alerted: dict = {}          # вилка -> когда последний раз о ней сообщали
    started = last_status = last_pub = time.time()
    try:
        while True:
            engine.wake.wait(0.25)
            engine.wake.clear()
            engine.reap()
            now = time.time()
            rows, ring = [], False
            while not engine.events.empty():
                kind, ep = engine.events.get()
                d = ep["snap"]
                if d is None:
                    continue
                if kind == "new":
                    # Сообщаем о новой вилке (или вернувшейся после минуты отсутствия).
                    if now - alerted.get(d["id"], 0) > 60:
                        print(f"\n[{time.strftime('%H:%M:%S', time.localtime(ep['start']))}] ВИЛКА "
                              + view.text(d), flush=True)
                        ring = True
                        if tg:
                            tg.send(d)
                    alerted[d["id"]] = now
                else:
                    lasted = ep["gone_at"] - ep["start"]
                    rows.append(view.csv_row(d, ep["start"], lasted, ep["peak"]))
                    if not ep["announced"]:     # всплеск короче --hold: только в журнал
                        continue
                    alerted[d["id"]] = ep["gone_at"]
                    print(f"[{time.strftime('%H:%M:%S')}] вилка закрылась: {d['match']} — "
                          f"{d['legs'][-1]['what']}; держалась {lasted:.1f} с, "
                          f"максимум {ep['peak'] * 100:+.2f}%", flush=True)
            if ring and args.sound:
                beep()
            if rows and args.csv:
                write_csv(args.csv, rows)
            if hub and now - last_pub >= 0.2:
                publish()
                last_pub = now
            if now - last_status >= args.status:
                last_status = now
                snap = engine.snapshot(args.near)
                print(status_line(snap) + f" | вилок сейчас: {len(snap['arbs'])}", flush=True)
                if args.near and snap["near"]:
                    print("  ближайшие: " + "; ".join(
                        f"{d['pct']:+.2f}% {d['match']} ({d['legs'][-1]['what']})"
                        for d in snap["near"]))
            if args.duration and now - started >= args.duration:
                break
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        if args.csv:
            now = time.time()
            rows = [view.csv_row(ep["snap"], ep["start"], (ep["gone_at"] or now) - ep["start"],
                                 ep["peak"]) for ep in engine.flush() if ep["snap"]]
            if rows:
                write_csv(args.csv, rows)


if __name__ == "__main__":
    main()
