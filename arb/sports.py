"""Единые названия видов спорта для обеих площадок.

Киберспортивные дисциплины определяются по названию, а не по номеру: у BC Game
(Betby) номера дисциплин нигде не описаны, а у Polymarket название игры стоит
в заголовке события («Counter-Strike: A vs B (BO3) - Турнир»), в тегах и в слаге.
Так новая дисциплина подхватывается сама, если её название есть в таблице ниже
или просто одинаково записано на обеих площадках.
"""
from __future__ import annotations

import re

# фрагмент нормализованного названия -> семейство. Порядок важен: первое совпадение.
ESPORT_NAMES = [
    ("counterstrike", "cs2"), ("cs2", "cs2"), ("csgo", "cs2"),
    ("dota", "dota2"),
    ("leagueoflegendswildrift", "wildrift"), ("wildrift", "wildrift"),
    ("leagueoflegends", "lol"), ("lol", "lol"),
    ("valorant", "valorant"), ("val", "valorant"),
    ("rainbowsix", "r6"), ("r6", "r6"),
    ("honorofkings", "hok"), ("kingofglory", "hok"), ("hok", "hok"),
    ("mobilelegends", "mlbb"), ("mlbb", "mlbb"),
    ("callofduty", "cod"), ("cod", "cod"),
    ("overwatch", "overwatch"), ("ow", "overwatch"),
    ("starcraft", "starcraft"), ("sc2", "starcraft"),
    ("rocketleague", "rocketleague"), ("rl", "rocketleague"),
    ("arenaofvalor", "aov"), ("aov", "aov"),
    ("pubg", "pubg"), ("apexlegends", "apex"), ("apex", "apex"),
    ("crossfire", "crossfire"), ("warcraft", "warcraft"),
    ("ageofempires", "aoe"), ("deadlock", "deadlock"), ("marvelrivals", "marvelrivals"),
    ("worldoftanks", "wot"), ("freefire", "freefire"), ("brawlstars", "brawlstars"),
    ("halo", "halo"), ("fortnite", "fortnite"), ("teamfighttactics", "tft"),
]
ESPORTS = {fam for _, fam in ESPORT_NAMES}
# Короткие коды совпадают только целиком (иначе «val» нашлось бы в «Valencia»).
_SHORT = {"cs2", "csgo", "lol", "val", "r6", "hok", "mlbb", "cod", "ow", "sc2", "rl", "aov"}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def esport_family(name: str) -> str | None:
    """Семейство дисциплины по названию игры, тегу или коду слага; None — не киберспорт."""
    n = norm(name)
    if not n:
        return None
    for key, fam in ESPORT_NAMES:
        if n == key or (key not in _SHORT and key in n):
            return fam
    return None


def is_esport(family: str) -> bool:
    return family in ESPORTS or family.startswith("esport:")
