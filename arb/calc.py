"""Расчёт вилок: ставка в BC Game против покупки токена на Polymarket."""
from __future__ import annotations

from dataclasses import dataclass

# Какие исходы BC Game вместе с одним токеном Polymarket покрывают все варианты.
# pm_key -> список наборов ключей BC (каждый набор — альтернативный способ закрыть плечо).
def bc_complements(pm_key: tuple) -> list[list[tuple]]:
    kind = pm_key[0]
    if kind == "ml":
        other = "c2" if pm_key[1] == "c1" else "c1"
        return [[("ml", other)]]
    if kind == "total":
        other = "under" if pm_key[2] == "over" else "over"
        return [[("total", pm_key[1], other)]]
    if kind == "hcp":
        other = "c2" if pm_key[2] == "c1" else "c1"
        return [[("hcp", pm_key[1], other)]]
    if kind == "1x2":            # PM: «Да» на исход X -> в BC нужны два других исхода
        rest = [o for o in ("c1", "draw", "c2") if o != pm_key[1]]
        dc = {"c1": "xc2", "draw": "c12", "c2": "c1x"}[pm_key[1]]
        return [[("dc", dc)], [("1x2", rest[0]), ("1x2", rest[1])]]
    if kind == "not1x2":         # PM: «Нет» на исход X -> в BC нужен сам X
        return [[("1x2", pm_key[1])]]
    return []


def pm_cost(price: float, fee_rate: float) -> float:
    """Полная цена одной акции с комиссией тейкера (выплата по акции = 1)."""
    return price + fee_rate * price * (1.0 - price)


@dataclass
class Arb:
    profit: float          # доля, 0.02 = 2% от суммарных вложений
    bc_inv: float          # сумма 1/k по плечу BC
    pm_price: float        # средняя цена акции PM без комиссии
    pm_cost: float         # средняя цена акции PM с комиссией
    shares: float          # сколько акций можно взять, пока вилка сохраняется
    total_stake: float     # суммарные вложения (USD) на этот объём
    profit_usd: float


def find_arb(bc_inv: float, asks: list[tuple[float, float]], fee_rate: float,
             min_profit: float = 0.0) -> Arb | None:
    """Идём по стакану, пока каждый следующий уровень ещё даёт прибыль >= min_profit.

    На N акций PM нужно поставить в BC сумму N * bc_inv (чтобы выплата была N
    при любом исходе). Вложения = N * (bc_inv + cost), выплата = N.
    """
    limit = 1.0 / (1.0 + min_profit)
    shares = 0.0
    spent = 0.0       # с комиссией
    raw = 0.0         # без комиссии
    for price, size in asks:
        cost = pm_cost(price, fee_rate)
        if bc_inv + cost >= limit or size <= 0:
            break
        shares += size
        spent += size * cost
        raw += size * price
    if shares <= 0:
        return None
    avg_cost = spent / shares
    total = shares * (bc_inv + avg_cost)
    return Arb(
        profit=shares / total - 1.0,
        bc_inv=bc_inv,
        pm_price=raw / shares,
        pm_cost=avg_cost,
        shares=shares,
        total_stake=total,
        profit_usd=shares - total,
    )
