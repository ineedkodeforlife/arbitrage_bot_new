"""Как связка выглядит для человека: подписи исходов, текст для консоли, строка CSV."""
from __future__ import annotations

import time

from .calc import pm_cost
from .model import Event
from .sports import is_esport

SUSPICIOUS = 0.15      # прибыль выше 15% почти всегда означает ошибку, а не вилку

CSV_HEAD = ["появилась", "держалась, с", "вид спорта", "команда 1", "команда 2", "начало",
            "плечо BC Game", "кф BC Game", "плечо Polymarket", "цена Polymarket",
            "доходность при появлении, %", "макс. доходность, %", "объём, $",
            "проверить вручную", "BC Game", "Polymarket"]


def label(key: tuple, ev: Event) -> str:
    team = {"c1": ev.c1, "c2": ev.c2, "draw": "ничья"}
    k = key[0]
    esport = is_esport(ev.sport)
    if k == "ml":
        return f"победа {team[key[1]]}"
    if k == "1x2":
        return team[key[1]] if key[1] == "draw" else f"победа {team[key[1]]} (осн. время)"
    if k == "not1x2":
        return f"НЕ {team[key[1]]}" if key[1] != "draw" else "НЕ ничья"
    if k == "dc":
        return {"c1x": f"{ev.c1} или ничья", "c12": "не ничья (12)",
                "xc2": f"ничья или {ev.c2}"}[key[1]]
    if k == "btts":
        return "обе забьют: " + ("да" if key[1] == "yes" else "нет")
    if k == "total":
        unit = " карт" if esport else " геймов" if ev.sport == "tennis" else ""
        return f"тотал{unit} {'больше' if key[2] == 'over' else 'меньше'} {key[1]:g}"
    if k in ("hcp", "sethcp"):
        line = key[1] if key[2] == "c1" else -key[1]
        unit = (" по картам" if esport else " по сетам" if k == "sethcp"
                else " по геймам" if ev.sport == "tennis" else "")
        return f"фора{unit} {team[key[2]]} ({line:+g})"
    if k == "map":
        return f"карта {key[1]}: победа {team[key[2]]}"
    if k == "set":
        return f"сет {key[1]}: победа {team[key[2]]}"
    return str(key)


def to_dict(c, bank: float, asks: list) -> dict | None:
    """Связка в виде словаря — для консоли, страницы в браузере и CSV.
    Вызывается под блокировкой движка. None — связку сейчас нельзя посчитать."""
    if c.bc is None or not c.legs or not asks:
        return None
    arb = c.arb
    edge = arb.profit if arb else c.edge
    cost = arb.pm_cost if arb else pm_cost(asks[0][0], c.sel.fee_rate)
    price = arb.pm_price if arb else asks[0][0]
    shares = bank / (c.bc_inv + cost)
    bc = c.bc
    when = time.strftime("%d.%m %H:%M", time.localtime(bc.start))
    if bc.live:
        when = "лайв " + " / ".join(x for x in (bc.info, c.pm.info) if x)
    legs = [{"site": "BC Game", "what": label(l, bc), "k": bc.odds[l],
             "price": f"кф {bc.odds[l]:g}", "stake": shares / bc.odds[l]} for l in c.legs]
    legs.append({"site": "Polymarket", "what": label(c.key, bc), "k": 1 / cost,
                 "price": f"цена {price:.3f} (кф {1 / cost:.3f} с комиссией)",
                 "stake": shares * cost})
    volume = (f"Объём по стакану: до ${arb.total_stake:,.0f}, прибыль до ${arb.profit_usd:,.2f}"
              if arb else f"На лучшей цене доступно {asks[0][1]:,.0f} акций")
    return {"id": f"{c.bc_id}|{c.sel.token}", "pct": round(edge * 100, 2),
            "match": f"{bc.c1} — {bc.c2}", "sport": bc.sport, "when": when, "live": bc.live,
            "start": bc.start, "c1": bc.c1, "c2": bc.c2,
            "suspicious": edge > SUSPICIOUS, "weak": c.weak,
            "pm_match": f"{c.pm.c1} — {c.pm.c2}",
            "legs": legs, "volume": volume, "pm_price": price,
            "total_stake": arb.total_stake if arb else 0.0,
            "question": c.sel.question, "bc_url": bc.url, "pm_url": c.pm.url}


def text(d: dict) -> str:
    flags = ""
    if d["suspicious"]:
        flags += "  [!] проверьте вручную — слишком большая доходность"
    out = [f"{d['pct']:+.2f}%  {d['match']}  [{d['sport']}, {d['when']}]{flags}"]
    if d["weak"]:
        out.append(f"   [!] матч сопоставлен по одной команде — убедитесь, что это он: "
                   f"на Polymarket «{d['pm_match']}»")
    for leg in d["legs"]:
        out.append(f"   {leg['site']:<10} {leg['what']:<40} {leg['price']:<38} ${leg['stake']:,.2f}")
    out.append(f"   {d['volume']}")
    out.append(f"   {d['question']}")
    out.append(f"   {d['bc_url']}\n   {d['pm_url']}")
    return "\n".join(out)


def csv_row(d: dict, started: float, lasted: float | None, peak: float) -> list:
    bc_legs = [l for l in d["legs"] if l["site"] == "BC Game"]
    pm_leg = d["legs"][-1]
    check = "; ".join(x for x in ("слишком большая доходность" if d["suspicious"] else "",
                                  f"матч по одной команде: {d['pm_match']}" if d["weak"] else "") if x)
    return [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "" if lasted is None else f"{lasted:.1f}",
            d["sport"], d["c1"], d["c2"],
            time.strftime("%Y-%m-%d %H:%M", time.localtime(d["start"])),
            " + ".join(l["what"] for l in bc_legs), " / ".join(f"{l['k']:g}" for l in bc_legs),
            pm_leg["what"], f"{d['pm_price']:.4f}", f"{d['pct']:.2f}", f"{peak * 100:.2f}",
            f"{d['total_stake']:.0f}", check, d["bc_url"], d["pm_url"]]
