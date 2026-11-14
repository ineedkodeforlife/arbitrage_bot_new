"""Спортивные рынки Polymarket: список игр из Gamma API.

API публичный и не требует ключей. Стаканы — в модуле books.py.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .http import POOL, fetch_json
from .match import name_score, tokens
from .model import Event, PmSel
from .sports import esport_family, is_esport, norm

GAMMA = "https://gamma-api.polymarket.com"
GAMES_TAG = 100639          # тег «Games» — матчи, а не долгосрочные рынки
DEFAULT_FEE = 0.05          # спорт: fee = shares * 0.05 * p * (1 - p)

# слаг тега события -> семейство вида спорта
TAG_FAMILY = {
    "soccer": "soccer", "basketball": "basketball", "nba": "basketball",
    "ncaa-basketball": "basketball", "hockey": "hockey", "nhl": "hockey",
    "tennis": "tennis", "atp": "tennis", "wta": "tennis",
    "nfl": "football", "cfb": "football", "football": "football",
    "baseball": "baseball", "mlb": "baseball", "ufc": "mma", "mma": "mma",
    "boxing": "boxing", "cricket": "cricket", "volleyball": "volleyball",
    "handball": "handball", "darts": "darts", "table-tennis": "tabletennis",
    "rugby": "rugby", "afl": "aussie", "aussie-rules": "aussie",
    "badminton": "badminton", "snooker": "snooker",
}
# Запасной вариант: префикс кода лиги в слаге события (bkfr1-..., cric...-, vb...-)
SLUG_PREFIX = [("cric", "cricket"), ("afl", "aussie"), ("bk", "basketball"),
               ("vb", "volleyball"), ("hb", "handball"), ("tt", "tabletennis"),
               ("ru", "rugby")]
# Матч приостановлен, перенесён или отменён — торги по нему неактуальны.
DEAD_PERIODS = ("SUS", "PPD", "CAN", "POST", "FT", "VFT")


def _ts(s: str | None) -> int:
    if not s:
        return 0
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _family(e: dict) -> str:
    slugs = [t.get("slug", "") for t in e.get("tags") or []]
    code = (e.get("slug") or "").split("-")[0]
    if "esports" in slugs:
        # название игры: теги, слаг серии, код слага, начало заголовка («Dota 2: ...»)
        names = slugs + [e.get("seriesSlug") or "", code, (e.get("title") or "").split(":")[0]]
        for n in names:
            fam = esport_family(n)
            if fam:
                return fam
        return "esport:" + norm((e.get("title") or "").split(":")[0] or code)
    for s in slugs:
        if s in TAG_FAMILY:
            return TAG_FAMILY[s]
    for prefix, fam in SLUG_PREFIX:
        if code.startswith(prefix) and len(code) > len(prefix):
            return fam
    return "other:" + code


def _jlist(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def _teams_from_title(title: str) -> tuple[str, str] | None:
    t = re.sub(r"\s+-\s+[^-]*$", "", title) if re.search(r"\s+-\s+(More Markets|Exact Score|"
                                                         r"Total Corners|Player Props|.*Props)$",
                                                         title) else title
    if ":" in t and " vs" in t.split(":", 1)[1]:
        t = t.split(":", 1)[1]      # «St. Tropez: A vs B», «Counter-Strike: A vs B (BO3) - ...»
    t = re.sub(r"\s*\(BO\d+\).*$", "", t, flags=re.I)
    m = re.split(r"\s+vs\.?\s+", t.strip(), maxsplit=1)
    if len(m) != 2:
        return None
    return m[0].strip(), m[1].strip()


def _teams(e: dict) -> tuple[str, str] | None:
    """Названия участников. Точнее всего они записаны в исходах рынка на победителя
    (там нет названия турнира и формата серии); для футбола берём из заголовка."""
    for m in e.get("markets") or []:
        if m.get("sportsMarketType") == "moneyline":
            out = _jlist(m.get("outcomes"))
            if len(out) == 2 and [o.strip().lower() for o in out] != ["yes", "no"]:
                return out[0].strip(), out[1].strip()
    return _teams_from_title(e.get("title") or "")


def _side(name: str, c1: str, c2: str) -> str | None:
    """К какой команде события относится название исхода."""
    if name.strip() == c1:
        return "c1"
    if name.strip() == c2:
        return "c2"
    n, a, b = tokens(name), tokens(c1), tokens(c2)
    s1, s2 = name_score(n, a), name_score(n, b)
    if max(s1, s2) < 0.8 or s1 == s2:
        return None
    return "c1" if s1 > s2 else "c2"


def _half(line) -> bool:
    return line is not None and (line * 2) % 2 == 1      # только x.5 — без возвратов


def _add_market(ev: Event, m: dict) -> None:
    if m.get("closed") or not m.get("acceptingOrders") or not m.get("enableOrderBook", True):
        return
    kind = m.get("sportsMarketType")
    outcomes = _jlist(m.get("outcomes"))
    toks = _jlist(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(toks) != 2:
        return
    fs = m.get("feeSchedule") or {}
    fee = float(fs.get("rate", DEFAULT_FEE)) if m.get("feesEnabled", True) else 0.0
    bid, ask = m.get("bestBid"), m.get("bestAsk")
    hints = [ask, (1 - bid) if bid else None]       # цена покупки исхода 0 и исхода 1
    q = m.get("question") or ""
    title = m.get("groupItemTitle") or q
    line = m.get("line")
    esport = is_esport(ev.sport)

    def put(key, idx):
        ev.sels[key] = PmSel(token=str(toks[idx]), question=f"{q} -> {outcomes[idx]}",
                             fee_rate=fee, hint_ask=hints[idx])

    def sides():
        s0 = _side(outcomes[0], ev.c1, ev.c2)
        s1 = _side(outcomes[1], ev.c1, ev.c2)
        return (s0, s1) if s0 and s1 and s0 != s1 else None

    def winner(prefix: tuple):
        ss = sides()
        if ss:
            put(prefix + (ss[0],), 0)
            put(prefix + (ss[1],), 1)

    def named_handicap(key_kind: str):
        """«... Handicap: A (-1.5) vs B (+1.5)»: первый исход — команда с форой line."""
        ss = sides()
        mm = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", q)
        if not ss or not _half(line) or not mm or abs(float(mm.group(1)) - float(line)) > 1e-9:
            return
        c1_line = float(line) if ss[0] == "c1" else -float(line)
        put((key_kind, c1_line, ss[0]), 0)
        put((key_kind, c1_line, ss[1]), 1)

    def over_under():
        low = [o.strip().lower() for o in outcomes]
        if _half(line) and low[0].startswith("over") and low[1].startswith("under"):
            put(("total", float(line), "over"), 0)
            put(("total", float(line), "under"), 1)

    low = [o.strip().lower() for o in outcomes]
    if kind == "moneyline":
        if low == ["yes", "no"]:
            # футбол: три отдельных рынка «победит X?» / «ничья?»
            if re.match(r"\s*draw\b", title, re.I) or "end in a draw" in q.lower():
                side = "draw"
            else:
                side = _side(title, ev.c1, ev.c2)
            if side:
                put(("1x2", side), 0)
                put(("not1x2", side), 1)
        else:
            winner(("ml",))
    elif kind == "child_moneyline" and esport:
        # «Map 2 Winner» (CS, Valorant) / «Game 2 Winner» (Dota 2, LoL)
        mm = re.search(r"\b(?:Map|Game)\s+(\d+)\s+Winner", title + " " + q, re.I)
        if mm:
            winner(("map", int(mm.group(1))))
    elif kind == "map_handicap" and esport:
        named_handicap("hcp")
    elif kind == "totals":
        # обычный тотал матча; в киберспорте — тотал карт. В теннисе и волейболе
        # «totals» означал бы сеты, которых в снимке BC нет, — такие ключи просто
        # не найдут пары.
        if ev.sport not in ("tennis", "volleyball", "hockey"):
            over_under()
    elif kind == "spreads":
        if not _half(line):
            return
        # «Spread: Hurricanes (-1.5)» — фора относится к названной команде
        mm = re.search(r"Spread:\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", q)
        ss = sides()
        if not mm or not ss or ev.sport in ("hockey", "volleyball", "tennis"):
            return
        named = _side(mm.group(1), ev.c1, ev.c2)
        if named is None or abs(float(mm.group(2)) - float(line)) > 1e-9:
            return
        c1_line = float(line) if named == "c1" else -float(line)
        put(("hcp", c1_line, ss[0]), 0)
        put(("hcp", c1_line, ss[1]), 1)
    elif kind == "both_teams_to_score" and ev.sport == "soccer":
        if low == ["yes", "no"]:
            put(("btts", "yes"), 0)
            put(("btts", "no"), 1)
    elif ev.sport == "tennis":
        if kind in ("tennis_first_set_winner", "tennis_set_winner"):
            mm = re.search(r"\bSet\s+(\d+)\s+Winner", q + " " + title, re.I)
            if mm:
                winner(("set", int(mm.group(1))))
        elif kind == "tennis_match_totals":
            over_under()                    # тотал геймов
        elif kind == "tennis_game_handicap":
            named_handicap("hcp")           # фора по геймам
        elif kind == "tennis_set_handicap":
            named_handicap("sethcp")


LIVE_BACK_HOURS = 5         # насколько давно мог начаться ещё идущий матч


def parse_events(raw: list[dict], hours: float = 48, live: bool | None = False,
                 now: float | None = None) -> list[Event]:
    """Разобрать ответ Gamma. live=False — только не начавшиеся матчи,
    True — только идущие, None — и те и другие."""
    now = int(now or time.time())
    horizon = now + int(hours * 3600)
    parents: dict[str, Event] = {}
    children: list[tuple[str, dict]] = []
    for e in raw:
        start = _ts(e.get("startTime")) or _ts(e.get("endDate"))
        if e.get("ended") or e.get("period") in DEAD_PERIODS:
            continue
        is_live = bool(e.get("live"))
        if (live is True and not is_live) or (live is False and is_live):
            continue
        if not is_live and start > horizon:
            continue
        if e.get("parentEventId"):
            children.append((str(e["parentEventId"]), e))
            continue
        teams = _teams(e)
        if not teams:
            continue
        if re.search(r"\(W\)", e.get("title") or ""):     # женский матч: пометка только в заголовке
            teams = tuple(t if "(W)" in t else t + " (W)" for t in teams)
        ev = Event(source="pm", id=str(e["id"]), sport=_family(e),
                   league=(e.get("seriesSlug") or (e.get("slug") or "").split("-")[0]),
                   c1=teams[0], c2=teams[1], start=start,
                   url=f"https://polymarket.com/event/{e.get('slug')}", live=is_live,
                   info=" ".join(str(x) for x in (e.get("score"), e.get("period"),
                                                  e.get("elapsed")) if x) if is_live else "")
        for m in e.get("markets") or []:
            _add_market(ev, m)
        parents[ev.id] = ev
    # тоталы и форы футбола лежат в дочернем событии «More Markets»
    for pid, e in children:
        ev = parents.get(pid)
        if ev:
            for m in e.get("markets") or []:
                _add_market(ev, m)
    return [ev for ev in parents.values() if ev.sels]


def fetch_raw(hours: float = 48, live: bool | None = False, verbose: bool = False,
              wave: int = 4, page: int = 100) -> list[dict]:
    """Скачать список игр. Страницы идут параллельно, пачками по wave штук:
    ответы тяжёлые (~90 КБ на событие), и это главная задержка при запуске."""
    real_now = int(time.time())
    since = real_now if live is False else real_now - LIVE_BACK_HOURS * 3600
    horizon = real_now if live is True else real_now + int(hours * 3600)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def get(offset: int):
        url = (f"{GAMMA}/events?tag_id={GAMES_TAG}&active=true&closed=false"
               f"&order=startTime&ascending=true&start_time_min={iso(since)}"
               f"&limit={page}&offset={offset}")
        try:
            return fetch_json(url, timeout=90)
        except RuntimeError as e:
            if "offset too large" in str(e):    # глубже API не листает
                return []
            raise

    out, offset = [], 0
    while True:
        batches = list(POOL.map(get, [offset + i * page for i in range(wave)]))
        done = False
        for batch in batches:
            out.extend(batch)
            if len(batch) < page or any((_ts(e.get("startTime")) or 0) > horizon for e in batch):
                done = True
        if verbose:
            print(f"  polymarket: получено событий {len(out)}")
        if done:
            return out
        offset += wave * page


def fetch_events(hours: float = 48, verbose: bool = False,
                 live: bool | None = False) -> list[Event]:
    return parse_events(fetch_raw(hours, live, verbose), hours, live)
