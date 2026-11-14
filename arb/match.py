"""Сопоставление событий BC Game и Polymarket по названиям команд и времени начала."""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from difflib import SequenceMatcher
from functools import lru_cache

from .model import Event
from .sports import is_esport

# Слова, которые не несут смысла для различения команд.
NOISE = {
    "fc", "cf", "sc", "ac", "afc", "cfc", "fk", "sk", "bk", "if", "bc", "cd", "ca",
    "club", "de", "the", "team", "esports", "esport", "gaming", "sv", "ss", "as", "us", "ud",
    "calcio", "futbol", "football", "hc", "hk", "ik", "kk", "basket", "ehc", "cb", "vc",
    "basquet", "basketball", "hockey", "volley", "volleyball",
}
# Слова, которые обязаны совпадать (иначе это другая команда: молодёжка, дубль и т.п.).
STRICT = re.compile(r"^(w|u\d{2}|ii|iii|b|reserves|youth|academy|junior|juniors)$")
WOMEN = {"w", "women", "womens", "woman", "ladies", "fem", "femenino", "feminino", "femenina",
         "feminine", "frauen", "damen", "wfc", "fcw"}
# Женский турнир по названию лиги: «wsl-games», «International / Women's T20».
WOMEN_LEAGUE = re.compile(r"(?<![a-z])(women|womens|woman|ladies|femin[a-z]*|femen[a-z]*|frauen|"
                          r"damen|wsl|nwsl|wnba|aflw|w league)(?![a-z])")
ALIASES = {
    "utd": "united", "man": "manchester", "st": "saint", "intl": "international",
    "munchen": "munich", "wolves": "wolverhampton", "spurs": "tottenham",
    "psg": "paris", "inter": "internazionale", "uconn": "connecticut",
}
# Только футбольные сокращения: в НБА «Spurs» и «Wolves» — другие команды.
SOCCER_ONLY = {"man", "wolves", "spurs", "inter", "psg"}
# Виды спорта, где играют люди, а не команды: инициалы вроде «B» — не признак дубля,
# а однофамильцы возможны, поэтому совпасть должны оба участника.
INDIVIDUAL = {"tennis", "tabletennis", "darts", "badminton", "snooker", "mma", "boxing"}

# Допуск по времени начала, секунды. В теннисе и единоборствах расписание плавает,
# киберспортивные серии часто задерживаются на часы. В настольном теннисе одни и те
# же игроки встречаются по нескольку раз в день — там допуск должен быть узким.
TIME_TOL = {"tennis": 12 * 3600, "mma": 12 * 3600, "boxing": 12 * 3600,
            "tabletennis": 45 * 60, "darts": 6 * 3600}
ESPORT_TOL = 6 * 3600
DEFAULT_TOL = 3 * 3600
WEAK_TOL = 5 * 60           # для совпадения по одной команде время должно сойтись точно
WEAK_SCORE = 0.5            # балл такого совпадения: сканер просит проверить его вручную


def time_tol(sport: str) -> int:
    if sport in TIME_TOL:
        return TIME_TOL[sport]
    return ESPORT_TOL if is_esport(sport) else DEFAULT_TOL


@lru_cache(maxsize=50000)
def tokens(name: str, sport: str = "") -> frozenset[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((w|women|f)\)", " w ", s)       # «Arsenal (W)» — женская команда
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    out = []
    for t in s.split():
        if t in WOMEN:
            t = "w"
        if re.fullmatch(r"(18|19|20)\d{2}|\d{2,4}", t) and not re.fullmatch(r"u\d{2}", t):
            continue  # годы основания: 1912, 04, 1860
        if t in ALIASES and (sport == "soccer" or t not in SOCCER_ONLY):
            t = ALIASES[t]
        if t in NOISE:
            continue
        out.append(t)
    return frozenset(out) or frozenset(s.split())


@lru_cache(maxsize=500000)
def name_score(a: frozenset[str], b: frozenset[str], strict: bool = True) -> float:
    """0..1. Высокий балл, если одно название содержится в другом."""
    if not a or not b:
        return 0.0
    if strict:
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
    sm = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b)))
    # дешёвые верхние оценки отсекают почти все пары до дорогого сравнения
    if sm.real_quick_ratio() < 0.82 or sm.quick_ratio() < 0.82:
        return 0.0
    ratio = sm.ratio()
    return ratio if ratio >= 0.82 else 0.0


def _related(a: frozenset, b: frozenset) -> bool:
    """Есть общее значимое слово: «UConn Huskies» / «Connecticut Huskies»,
    «Castanet» / «Castaneenne» (общее начало от 5 букв)."""
    if {t for t in a if STRICT.match(t)} != {t for t in b if STRICT.match(t)}:
        return False                # основа и академия / дубль — разные команды
    for x in a:
        if len(x) < 4 or STRICT.match(x):
            continue
        for y in b:
            if x == y or (len(y) >= 5 and x[:5] == y[:5]):
                return True
    return False


def _women(e: Event, t1: frozenset, t2: frozenset) -> bool:
    if "w" in t1 or "w" in t2:
        return True
    league = re.sub(r"[^a-z]+", " ", e.league.lower().replace("'", ""))
    return bool(WOMEN_LEAGUE.search(league))


def match_events(bc_events: list[Event], pm_events: list[Event], min_score: float = 0.85):
    """-> список (bc, pm, flipped, score). Каждое событие используется один раз.

    score >= min_score — совпали обе команды. score == WEAK_SCORE — уверенно совпала
    одна команда, точно совпало время начала, а у второй команды есть общее слово
    (другое написание: «UConn Huskies» и «Connecticut Huskies»); такие пары сканер помечает.
    """
    cache: dict = {}

    def info(e: Event):
        """(токены c1, токены c2, женский матч?) — без пометки «w» в самих названиях."""
        k = (e.source, e.id)
        v = cache.get(k)
        if v is None:
            t1, t2 = tokens(e.c1, e.sport), tokens(e.c2, e.sport)
            if e.sport in INDIVIDUAL:
                v = (t1, t2, False)
            else:
                v = (t1 - {"w"} or t1, t2 - {"w"} or t2, _women(e, t1, t2))
            cache[k] = v
        return v

    by_sport: dict[str, list[Event]] = {}
    for e in bc_events:
        by_sport.setdefault(e.sport, []).append(e)
    for lst in by_sport.values():
        lst.sort(key=lambda e: e.start)
    starts = {sp: [e.start for e in lst] for sp, lst in by_sport.items()}

    cands, weak = [], []
    for pm in pm_events:
        lst = by_sport.get(pm.sport)
        if not lst:
            continue
        tol = time_tol(pm.sport)
        strict = pm.sport not in INDIVIDUAL
        p1, p2, pw = info(pm)
        lo = bisect_left(starts[pm.sport], pm.start - tol)
        hi = bisect_right(starts[pm.sport], pm.start + tol)
        for bc in lst[lo:hi]:
            b1, b2, bw = info(bc)
            if bw != pw:
                continue
            d1, d2 = name_score(b1, p1, strict), name_score(b2, p2, strict)
            x1, x2 = name_score(b1, p2, strict), name_score(b2, p1, strict)
            direct, cross = min(d1, d2), min(x1, x2)
            score, flipped = (cross, True) if cross > direct else (direct, False)
            dt = abs(bc.start - pm.start)
            if score >= min_score:
                cands.append((score, -dt, bc, pm, flipped))
            elif strict and dt <= WEAK_TOL:
                # одна команда совпала уверенно, а у второй есть общее слово
                for s, other, fl in ((d1, (b2, p2), False), (d2, (b1, p1), False),
                                     (x1, (b2, p1), True), (x2, (b1, p2), True)):
                    if s >= 0.9 and _related(*other):
                        weak.append((bc, pm, fl))
                        break
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)

    used_bc, used_pm, out = set(), set(), []
    for score, _, bc, pm, flipped in cands:
        if bc.id in used_bc or pm.id in used_pm:
            continue
        used_bc.add(bc.id)
        used_pm.add(pm.id)
        out.append((bc, pm, flipped, score))
    # Совпадение по одной команде принимаем, только если оно единственное с обеих сторон.
    n_bc: dict = {}
    n_pm: dict = {}
    for bc, pm, _ in weak:
        if bc.id not in used_bc and pm.id not in used_pm:
            n_bc[bc.id] = n_bc.get(bc.id, 0) + 1
            n_pm[pm.id] = n_pm.get(pm.id, 0) + 1
    for bc, pm, flipped in weak:
        if n_bc.get(bc.id) == 1 and n_pm.get(pm.id) == 1:
            out.append((bc, pm, flipped, WEAK_SCORE))
    return out


def flip_key(key: tuple) -> tuple:
    """Перевести ключ исхода из порядка команд Polymarket в порядок BC Game (c1 <-> c2)."""
    sw = {"c1": "c2", "c2": "c1", "draw": "draw"}
    kind = key[0]
    if kind in ("ml", "1x2", "not1x2"):
        return (kind, sw[key[1]])
    if kind in ("hcp", "sethcp"):
        return (kind, -key[1], sw[key[2]])
    if kind in ("map", "set"):
        return (kind, key[1], sw[key[2]])
    return key
