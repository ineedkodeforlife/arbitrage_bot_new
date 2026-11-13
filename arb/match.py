"""Сопоставление событий BC Game и Polymarket по названиям команд и времени начала."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

from .model import Event

# Слова, которые не несут смысла для различения команд.
NOISE = {
    "fc", "cf", "sc", "ac", "afc", "cfc", "fk", "sk", "bk", "if", "bc", "cd", "ca",
    "club", "de", "the", "team", "esports", "gaming", "sv", "ss", "as", "us", "ud",
    "calcio", "futbol", "football", "hc", "hk", "ik", "kk", "bk", "basket",
}
# Слова, которые обязаны совпадать (иначе это другая команда: женская, молодёжка и т.п.).
STRICT = re.compile(r"^(w|women|womens|ladies|fem|femenino|u\d{2}|ii|iii|b|reserves|youth|academy)$")
ALIASES = {
    "utd": "united", "man": "manchester", "st": "saint", "intl": "international",
    "munchen": "munich", "wolves": "wolverhampton", "spurs": "tottenham",
    "psg": "paris", "inter": "internazionale",
}

# Допуск по времени начала, секунды. В теннисе и единоборствах расписание плавает.
TIME_TOL = {"tennis": 12 * 3600, "mma": 12 * 3600, "boxing": 12 * 3600}
DEFAULT_TOL = 3 * 3600


@lru_cache(maxsize=50000)
def tokens(name: str, sport: str = "") -> frozenset[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    out = []
    for t in s.split():
        if re.fullmatch(r"(18|19|20)\d{2}|\d{2,4}", t) and not re.fullmatch(r"u\d{2}", t):
            continue  # годы основания: 1912, 04, 1860
        if sport == "soccer":
            t = ALIASES.get(t, t)
        if t in NOISE:
            continue
        out.append(t)
    return frozenset(out) or frozenset(s.split())


@lru_cache(maxsize=500000)
def name_score(a: frozenset[str], b: frozenset[str]) -> float:
    """0..1. Высокий балл, если одно название содержится в другом."""
    if not a or not b:
        return 0.0
    sa = {t for t in a if STRICT.match(t)}
    sb = {t for t in b if STRICT.match(t)}
    if sa != sb:
        return 0.0
    if a == b:
        return 1.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    core = {t for t in small if len(t) >= 3}

    def has(t: str) -> bool:
        # «linkoping» ~ «linkopings», «timra» ~ «timraa»
        return t in big or (len(t) >= 5 and any(
            len(x) >= 5 and (x.startswith(t) or t.startswith(x)) for x in big))

    if core and all(has(t) for t in core):
        # «Spurs» ⊂ «San Antonio Spurs», «Mayot Harold» == «Harold Mayot»
        return 0.9
    # Инициалы в теннисе: «Mayot H.» против «Harold Mayot»
    long_small = {t for t in small if len(t) > 1}
    if long_small and long_small <= big and all(
        any(x.startswith(t) for x in big) for t in small if len(t) == 1
    ):
        return 0.85
    ratio = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return ratio if ratio >= 0.82 else 0.0


def pair_score(bc: Event, pm: Event, tok) -> tuple[float, bool]:
    """Возвращает (балл, flipped). flipped=True — c1 Polymarket это c2 BC Game."""
    b1, b2, p1, p2 = tok(bc, 1), tok(bc, 2), tok(pm, 1), tok(pm, 2)
    direct = min(name_score(b1, p1), name_score(b2, p2))
    cross = min(name_score(b1, p2), name_score(b2, p1))
    return (cross, True) if cross > direct else (direct, False)


def match_events(bc_events: list[Event], pm_events: list[Event], min_score: float = 0.85):
    """-> список (bc, pm, flipped, score). Каждое событие используется один раз."""
    cache: dict = {}

    def tok(e: Event, n: int):
        k = (e.source, e.id, n)
        if k not in cache:
            cache[k] = tokens(e.c1 if n == 1 else e.c2, e.sport)
        return cache[k]

    by_sport: dict[str, list[Event]] = {}
    for e in bc_events:
        by_sport.setdefault(e.sport, []).append(e)

    cands = []
    for pm in pm_events:
        tol = TIME_TOL.get(pm.sport, DEFAULT_TOL)
        for bc in by_sport.get(pm.sport, []):
            if abs(bc.start - pm.start) > tol:
                continue
            score, flipped = pair_score(bc, pm, tok)
            if score >= min_score:
                cands.append((score, -abs(bc.start - pm.start), bc, pm, flipped))
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)

    used_bc, used_pm, out = set(), set(), []
    for score, _, bc, pm, flipped in cands:
        if bc.id in used_bc or pm.id in used_pm:
            continue
        used_bc.add(bc.id)
        used_pm.add(pm.id)
        out.append((bc, pm, flipped, score))
    return out


def flip_key(key: tuple) -> tuple:
    """Перевести ключ исхода из порядка команд Polymarket в порядок BC Game (c1 <-> c2)."""
    sw = {"c1": "c2", "c2": "c1", "draw": "draw"}
    kind = key[0]
    if kind in ("ml", "1x2", "not1x2"):
        return (kind, sw[key[1]])
    if kind == "hcp":
        return ("hcp", -key[1], sw[key[2]])
    return key
