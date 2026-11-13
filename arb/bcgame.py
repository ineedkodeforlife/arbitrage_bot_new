"""Прематч-линия BC Game.

Спортбук BC Game работает на движке Betby; линия отдаётся обычными GET-запросами
с хоста *.sptpub.com. Запрос версии 0 возвращает список актуальных версий-частей,
каждая часть — снимок событий с основными рынками. Идентификаторы рынков и исходов
совпадают с нумерацией Sportradar (1 = 1x2, 18 = тотал, 16 = фора и т.д.).
"""
from __future__ import annotations

from .http import POOL, curl_json
from .model import Event

# Хост и brand_id видны в сетевых запросах страницы bc.game/sports. Если BC Game
# сменит хост, достаточно поправить эти две константы.
API = "https://api-k-c7818b61-623.sptpub.com"
BRAND = "2103509236163162112"
HEADERS = {"Origin": "https://bc.game", "Referer": "https://bc.game/"}

# id вида спорта Betby -> семейство
SPORTS = {
    "1": "soccer", "2": "basketball", "3": "baseball", "4": "hockey", "5": "tennis",
    "16": "football", "117": "mma", "10": "boxing", "21": "cricket", "23": "volleyball",
    "6": "handball", "22": "darts", "20": "tabletennis", "12": "rugby",
    "109": "cs2", "111": "dota2", "110": "lol", "194": "valorant",
}

# Какие рынки берём для каждого семейства: market_id -> тип.
# Берём только те, чьи правила расчёта совпадают с Polymarket:
# футбол — основное время; баскетбол/ам. футбол/бейсбол — с овертаймом;
# хоккей — только победитель с ОТ и буллитами (тотал в снимке — на основное время).
ML, X12, DC, TOT, HCP = "ml", "1x2", "dc", "total", "hcp"
MARKETS = {
    "soccer": {"1": X12, "10": DC, "18": TOT, "16": HCP},
    "basketball": {"219": ML, "225": TOT, "223": HCP},
    "football": {"219": ML, "225": TOT, "223": HCP},
    "baseball": {"251": ML, "258": TOT, "256": HCP},
    "hockey": {"406": ML},
    "cricket": {"340": ML},
}
DEFAULT_MARKETS = {"186": ML}   # теннис, единоборства, киберспорт и т.п.

OUTCOMES = {
    ML: {"4": "c1", "5": "c2"},
    X12: {"1": "c1", "2": "draw", "3": "c2"},
    DC: {"9": "c1x", "10": "c12", "11": "xc2"},
    TOT: {"12": "over", "13": "under"},
    HCP: {"1714": "c1", "1715": "c2"},
}


def _line(spec: str, name: str) -> float | None:
    for part in spec.split("&"):
        k, _, v = part.partition("=")
        if k == name:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def _parse_markets(sport: str, markets: dict) -> dict:
    odds = {}
    wanted = MARKETS.get(sport, DEFAULT_MARKETS)
    for mid, kind in wanted.items():
        for spec, outcomes in (markets.get(mid) or {}).items():
            line = None
            if kind == TOT:
                line = _line(spec, "total")
            elif kind == HCP:
                line = _line(spec, "hcp")
            elif spec:
                continue            # вариативные рынки (по сетам и пр.) не нужны
            if kind in (TOT, HCP) and line is None:
                continue
            for oid, o in outcomes.items():
                side = OUTCOMES[kind].get(oid)
                if side is None or o.get("b"):      # b = исход заблокирован (лайв)
                    continue
                try:
                    k = float(o["k"])
                except (KeyError, TypeError, ValueError):
                    continue
                if k <= 1.0:
                    continue
                key = (kind, line, side) if line is not None else (kind, side)
                odds[key] = k
    return odds


def _get(mode: str, version) -> dict:
    return curl_json(f"{API}/api/v4/{mode}/brand/{BRAND}/en/{version}", headers=HEADERS)


class Feed:
    """Линия BC Game: первый вызов sync() качает полный снимок, следующие — только
    изменения с прошлого раза (каждый ответ сервера содержит номер следующей версии).
    Так же обновляется сама страница bc.game; изменения появляются раз в ~2 секунды.
    """

    def __init__(self, live: bool):
        self.live = live
        self.mode = "live" if live else "prematch"
        self.version = None
        self.epoch = None
        self.raw: dict = {}
        self.sports: dict = {}
        self.cats: dict = {}
        self.tours: dict = {}
        self._parsed: dict = {}      # id события -> Event (пересобираем только изменившиеся)

    def _apply(self, part: dict) -> None:
        self.sports.update(part.get("sports") or {})
        self.cats.update(part.get("categories") or {})
        self.tours.update(part.get("tournaments") or {})
        for eid, e in (part.get("events") or {}).items():
            self._parsed.pop(eid, None)
            if e is None:                       # событие снято с линии
                self.raw.pop(eid, None)
                continue
            cur = self.raw.setdefault(eid, {})
            for key, val in e.items():
                if key == "markets" and isinstance(val, dict):
                    mk = cur.setdefault("markets", {})
                    for mid, m in val.items():  # рынок приходит целиком или None
                        if m is None:
                            mk.pop(mid, None)
                        else:
                            mk[mid] = m
                else:
                    cur[key] = val

    def _snapshot(self) -> None:
        self.raw, self._parsed = {}, {}
        manifest = _get(self.mode, 0)
        self.epoch = manifest.get("epoch")
        versions = list(manifest.get("top_events_versions", [])) + \
            list(manifest.get("rest_events_versions", []))
        parts = list(POOL.map(lambda v: _get(self.mode, v), versions))
        for p in parts:
            self._apply(p)
        self.version = parts[-1].get("version") if parts else None

    def sync(self) -> list[Event]:
        if self.version is None:
            self._snapshot()
        else:
            d = _get(self.mode, self.version)
            if d.get("epoch") not in (None, self.epoch):
                self._snapshot()                # сервер начал новую эпоху — всё заново
            else:
                self._apply(d)
                self.version = d.get("version") or self.version
        return self.events()

    def events(self) -> list[Event]:
        out = []
        for eid, e in self.raw.items():
            ev = self._parsed.get(eid)
            if ev is None:
                ev = self._parsed[eid] = self._build(eid, e) or False
            if ev:
                out.append(ev)
        return out

    def _build(self, eid: str, e: dict) -> Event | None:
        d = e.get("desc") or {}
        comps = d.get("competitors") or []
        if d.get("type") != "match" or len(comps) != 2 or d.get("virtual"):
            return None
        sport = SPORTS.get(str(d.get("sport")))
        if not sport:
            return None
        odds = _parse_markets(sport, e.get("markets") or {})
        if not odds:
            return None
        cat = self.cats.get(d.get("category"), {})
        tour = self.tours.get(d.get("tournament"), {})
        s_slug = self.sports.get(str(d.get("sport")), {}).get("slug", "")
        path = "/".join(x for x in (s_slug, cat.get("slug"), tour.get("slug"),
                                    f"{d.get('slug')}-{eid}") if x)
        clock = ((e.get("state") or {}).get("clock") or {}).get("match_time", "")
        return Event(
            source="bc", id=eid, sport=sport,
            league=f"{cat.get('name', '')} / {tour.get('name', '')}".strip(" /"),
            c1=comps[0]["name"], c2=comps[1]["name"],
            start=int(d.get("scheduled") or 0),
            url=f"https://bc.game/sports?bt-path=/{path}",
            live=self.live, info=clock if self.live else "",
            odds=odds,
        )


def fetch_events(live: bool = False) -> list[Event]:
    """Разовый полный снимок линии."""
    return Feed(live).sync()
