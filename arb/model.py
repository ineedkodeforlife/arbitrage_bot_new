"""Общая модель данных для обеих площадок.

Ключ исхода (selection key) — кортеж, одинаковый для BC Game и Polymarket:
    ("ml", "c1" | "c2")                  победа с учётом ОТ / победитель матча (2 исхода)
    ("1x2", "c1" | "draw" | "c2")        основное время, 3 исхода
    ("dc", "c1x" | "c12" | "xc2")        двойной шанс (только BC Game)
    ("total", line, "over" | "under")    тотал
    ("hcp", line, "c1" | "c2")           фора; line — фора ПЕРВОЙ команды (c1),
                                         исход "c2" означает c2 с форой -line

c1 / c2 — это порядок команд внутри конкретного Event. При сопоставлении событий
разных площадок порядок может не совпасть, тогда ключи второй площадки
переворачиваются (см. match.flip_key).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Event:
    source: str            # "bc" | "pm"
    id: str
    sport: str             # семейство: soccer, basketball, hockey, tennis, ...
    league: str
    c1: str
    c2: str
    start: int             # unix time, UTC
    url: str = ""
    live: bool = False
    info: str = ""         # для лайва: время матча / период
    # BC: key -> десятичный коэффициент
    odds: dict = field(default_factory=dict)
    # PM: key -> PmSel
    sels: dict = field(default_factory=dict)


@dataclass
class PmSel:
    """Токен Polymarket, покупка которого = ставка на исход."""
    token: str
    question: str
    fee_rate: float        # комиссия тейкера: fee = shares * rate * p * (1 - p)
    hint_ask: float | None = None   # цена из Gamma (может быть устаревшей)
    asks: list = field(default_factory=list)  # [(price, size)] по возрастанию цены
