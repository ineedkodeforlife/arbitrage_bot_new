"""Спортивные рынки Polymarket: список игр из Gamma API, стаканы из CLOB API.

Оба API публичные и не требуют ключей.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .http import POOL, fetch_json
from .match import name_score, tokens
from .model import Event, PmSel

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
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
    "rugby": "rugby", "counter-strike-2": "cs2", "cs2": "cs2", "dota-2": "dota2",
    "league-of-legends": "lol", "lol": "lol", "valorant": "valorant",
}
# Запасной вариант: префикс кода лиги в слаге события (bkfr1-..., cric...-, vb...-)
SLUG_PREFIX = [("cric", "cricket"), ("bk", "basketball"), ("vb", "volleyball"),
               ("hb", "handball"), ("tt", "tabletennis"), ("ru", "rugby")]


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
    for s in slugs:
        if s in TAG_FAMILY:
            return TAG_FAMILY[s]
    code = (e.get("slug") or "").split("-")[0]
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
        t = t.split(":", 1)[1]      # «St. Tropez: A vs B»
    m = re.split(r"\s+vs\.?\s+", t.strip(), maxsplit=1)
    if len(m) != 2:
        return None
    return m[0].strip(), m[1].strip()


def _side(name: str, c1: str, c2: str) -> str | None:
    """К какой команде события относится название исхода."""
    n, a, b = tokens(name), tokens(c1), tokens(c2)
    s1, s2 = name_score(n, a), name_score(n, b)
    if max(s1, s2) < 0.8 or s1 == s2:
        return None
    return "c1" if s1 > s2 else "c2"


def _add_market(ev: Event, m: dict) -> None:
    if m.get("closed") or not m.get("acceptingOrders") or not m.get("enableOrderBook", True):
        return
    kind = m.get("sportsMarketType")
    if kind not in ("moneyline", "totals", "spreads"):
        return
    outcomes = _jlist(m.get("outcomes"))
    toks = _jlist(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(toks) != 2:
        return
    fs = m.get("feeSchedule") or {}
    fee = float(fs.get("rate", DEFAULT_FEE)) if m.get("feesEnabled", True) else 0.0
    bid, ask = m.get("bestBid"), m.get("bestAsk")
    hints = [ask, (1 - bid) if bid else None]       # цена покупки исхода 0 и исхода 1
    q = m.get("question") or ""

    def put(key, idx):
        ev.sels[key] = PmSel(token=str(toks[idx]), question=f"{q} -> {outcomes[idx]}",
                             fee_rate=fee, hint_ask=hints[idx])

    low = [o.strip().lower() for o in outcomes]
    if kind == "moneyline":
        if low == ["yes", "no"]:
            # футбол: три отдельных рынка «победит X?» / «ничья?»
            title = m.get("groupItemTitle") or q
            if re.match(r"\s*draw\b", title, re.I) or "end in a draw" in q.lower():
                side = "draw"
            else:
                side = _side(title, ev.c1, ev.c2)
            if side:
                put(("1x2", side), 0)
                put(("not1x2", side), 1)
        else:
            s0 = _side(outcomes[0], ev.c1, ev.c2)
            s1 = _side(outcomes[1], ev.c1, ev.c2)
            if s0 and s1 and s0 != s1:
                put(("ml", s0), 0)
                put(("ml", s1), 1)
    elif kind == "totals":
        line = m.get("line")
        if line is None or (line * 2) % 2 != 1:      # только x.5 — без возвратов
            return
        if low[0].startswith("over") and low[1].startswith("under"):
            put(("total", float(line), "over"), 0)
            put(("total", float(line), "under"), 1)
    elif kind == "spreads":
        line = m.get("line")
        if line is None or (line * 2) % 2 != 1:
            return
        # «Spread: Hurricanes (-1.5)» — фора относится к названной команде
        mm = re.search(r"Spread:\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", q)
        s0 = _side(outcomes[0], ev.c1, ev.c2)
        s1 = _side(outcomes[1], ev.c1, ev.c2)
        if not mm or not s0 or not s1 or s0 == s1:
            return
        named = _side(mm.group(1), ev.c1, ev.c2)
        if named is None or abs(float(mm.group(2)) - float(line)) > 1e-9:
            return
        c1_line = float(line) if named == "c1" else -float(line)
        put(("hcp", c1_line, s0), 0)
        put(("hcp", c1_line, s1), 1)


LIVE_BACK_HOURS = 5         # насколько давно мог начаться ещё идущий матч


def fetch_events(hours: float = 48, verbose: bool = False, live: bool = False) -> list[Event]:
    """live=False — только не начавшиеся матчи; live=True — только идущие сейчас."""
    real_now = int(time.time())
    now = real_now - LIVE_BACK_HOURS * 3600 if live else real_now
    horizon = real_now if live else real_now + int(hours * 3600)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    parents: dict[str, Event] = {}
    children: list[tuple[str, dict]] = []
    offset, page = 0, 100
    while True:
        url = (f"{GAMMA}/events?tag_id={GAMES_TAG}&active=true&closed=false"
               f"&order=startTime&ascending=true&start_time_min={iso(now)}"
               f"&limit={page}&offset={offset}")
        batch = fetch_json(url, timeout=90)
        if verbose:
            print(f"  polymarket: страница {offset // page + 1}, событий {len(batch)}")
        done = len(batch) < page
        for e in batch:
            start = _ts(e.get("startTime")) or _ts(e.get("endDate"))
            if start > horizon:
                done = True
                continue
            if e.get("ended"):
                continue
            if live:
                # SUS/PPD — матч приостановлен или перенесён, торги по нему неактуальны
                if not e.get("live") or e.get("period") in ("SUS", "PPD", "CAN"):
                    continue
            elif e.get("live"):
                continue
            if e.get("parentEventId"):
                children.append((str(e["parentEventId"]), e))
                continue
            teams = _teams_from_title(e.get("title") or "")
            if not teams:
                continue
            ev = Event(source="pm", id=str(e["id"]), sport=_family(e),
                       league=(e.get("seriesSlug") or (e.get("slug") or "").split("-")[0]),
                       c1=teams[0], c2=teams[1], start=start,
                       url=f"https://polymarket.com/event/{e.get('slug')}", live=live,
                       info=" ".join(str(x) for x in (e.get("score"), e.get("period"),
                                                      e.get("elapsed")) if x))
            for m in e.get("markets") or []:
                _add_market(ev, m)
            parents[ev.id] = ev
        if done:
            break
        offset += page
    # тоталы и форы футбола лежат в дочернем событии «More Markets»
    for pid, e in children:
        ev = parents.get(pid)
        if ev:
            for m in e.get("markets") or []:
                _add_market(ev, m)
    return [ev for ev in parents.values() if ev.sels]


def load_books(sels: list[PmSel], chunk: int = 100) -> None:
    """Заполнить sel.asks реальным стаканом."""
    by_token = {s.token: s for s in sels}
    ids = list(by_token)
    parts = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
    for s in sels:
        s.asks = []             # не оставляем стакан с прошлого прохода
    results = list(POOL.map(
        lambda part: fetch_json(f"{CLOB}/books", body=[{"token_id": t} for t in part],
                                timeout=10), parts))
    for books in results:
        for b in books or []:
            s = by_token.get(str(b.get("asset_id")))
            if s is None:
                continue
            asks = []
            for lvl in b.get("asks") or []:
                try:
                    asks.append((float(lvl["price"]), float(lvl["size"])))
                except (KeyError, TypeError, ValueError):
                    pass
            s.asks = sorted(asks)
