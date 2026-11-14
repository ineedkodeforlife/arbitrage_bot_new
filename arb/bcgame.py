"""Линия BC Game: прематч и лайв.

Спортбук BC Game работает на движке Betby; линия отдаётся обычными GET-запросами
с хоста *.sptpub.com. Запрос версии 0 возвращает список актуальных версий-частей,
каждая часть — снимок событий с основными рынками. Идентификаторы рынков и исходов
совпадают с нумерацией Sportradar (1 = 1x2, 18 = тотал, 16 = фора и т.д.).

Дальше линия читается как поток: ответ на запрос версии V содержит изменения и
номер следующей версии. Сервер выпускает одну версию в секунду; запрос самой
свежей версии сервер держит открытым, пока она не будет готова (long-poll),
поэтому изменения приходят через 0,2–0,6 с после появления, без опроса по таймеру.
"""
from __future__ import annotations

import threading
import time

from . import http
from .http import POOL, curl_json, fetch_json
from .model import Event
from .sports import esport_family, is_esport

# Хост и brand_id видны в сетевых запросах страницы bc.game/sports. Если BC Game
# сменит хост, достаточно поправить эти две константы.
API = "https://api-k-c7818b61-623.sptpub.com"
BRAND = "2103509236163162112"
HEADERS = {"Origin": "https://bc.game", "Referer": "https://bc.game/"}

# id вида спорта Betby -> семейство. Киберспорт определяется по названию (sports.py).
SPORTS = {
    "1": "soccer", "2": "basketball", "3": "baseball", "4": "hockey", "5": "tennis",
    "16": "football", "117": "mma", "10": "boxing", "21": "cricket", "23": "volleyball",
    "22": "darts", "20": "tabletennis", "13": "aussie", "31": "badminton", "19": "snooker",
}
# Не сопоставляем: гандбол, регби, флорбол и т.п. — в BC там только 1X2 основного
# времени, а Polymarket даёт победителя с учётом дополнительного времени.
# Симуляторы (eSoccer, eBasketball, NBA 2K, FC) — это не матчи реальных команд.

# Какие рынки берём для каждого семейства: market_id -> тип.
# Берём только те, чьи правила расчёта совпадают с Polymarket:
# футбол — основное время; баскетбол/ам. футбол/бейсбол — с овертаймом;
# хоккей — только победитель с ОТ и буллитами (тотал в снимке — на основное время).
ML, X12, DC, TOT, HCP, BTTS = "ml", "1x2", "dc", "total", "hcp", "btts"
MAP, SET, SETHCP = "map", "set", "sethcp"
MARKETS = {
    "soccer": {"1": X12, "10": DC, "18": TOT, "16": HCP, "29": BTTS},
    "basketball": {"219": ML, "225": TOT, "223": HCP},
    "football": {"219": ML, "225": TOT, "223": HCP},
    "aussie": {"219": ML},
    "baseball": {"251": ML, "258": TOT, "256": HCP},
    "hockey": {"406": ML},
    "cricket": {"340": ML},
    # теннис: победитель, победитель сета, тотал геймов, фора по геймам, фора по сетам
    "tennis": {"186": ML, "202": SET, "189": TOT, "187": HCP, "188": SETHCP},
}
# киберспорт: победитель матча, победитель карты, тотал карт, фора по картам
ESPORT_MARKETS = {"186": ML, "330": MAP, "328": TOT, "327": HCP}
DEFAULT_MARKETS = {"186": ML}   # единоборства, волейбол, дартс, настольный теннис и т.п.

OUTCOMES = {
    ML: {"4": "c1", "5": "c2"},
    X12: {"1": "c1", "2": "draw", "3": "c2"},
    DC: {"9": "c1x", "10": "c12", "11": "xc2"},
    TOT: {"12": "over", "13": "under"},
    HCP: {"1714": "c1", "1715": "c2"},
    SETHCP: {"1714": "c1", "1715": "c2"},
    BTTS: {"74": "yes", "76": "no"},
    MAP: {"4": "c1", "5": "c2"},
    SET: {"4": "c1", "5": "c2"},
}
# имя параметра рынка (specifier), из которого берётся линия / номер карты
SPEC = {TOT: "total", HCP: "hcp", SETHCP: "hcp", MAP: "mapnr", SET: "setnr"}


def _spec(spec: str) -> dict:
    out = {}
    for part in spec.replace("|", "&").split("&"):
        k, _, v = part.partition("=")
        if k:
            out[k] = v
    return out


def _parse_markets(sport: str, markets: dict) -> dict:
    odds = {}
    wanted = MARKETS.get(sport) or (ESPORT_MARKETS if is_esport(sport) else DEFAULT_MARKETS)
    for mid, kind in wanted.items():
        name = SPEC.get(kind)
        for spec, outcomes in (markets.get(mid) or {}).items():
            param = None
            if name is None:
                if spec:
                    continue        # вариативные рынки не нужны
            else:
                sp = _spec(spec)
                if list(sp) != [name]:
                    continue
                try:
                    param = float(sp[name])
                except ValueError:
                    continue
                if kind in (MAP, SET):
                    param = int(param)
                elif (param * 2) % 2 != 1:
                    continue        # только линии x.5 — без возвратов, как на Polymarket
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
                key = (kind, param, side) if param is not None else (kind, side)
                odds[key] = k
    return odds


# --- транспорт -------------------------------------------------------------------
# Порядок попыток: постоянное соединение через прокси (если он есть), напрямую,
# и запасной вариант — запуск curl на каждый запрос (медленнее на ~300 мс).
_transport: dict = {"kind": None}
_tlock = threading.Lock()


def _try(kind: str, url: str, timeout: float):
    if kind == "proxy":
        return fetch_json(url, headers=HEADERS, timeout=timeout, retries=0,
                          proxy=http.detect_proxy())
    if kind == "direct":
        return fetch_json(url, headers=HEADERS, timeout=timeout, retries=0)
    return curl_json(url, headers=HEADERS, timeout=timeout)


def _choose_transport() -> str:
    """Проверяем транспорт на большом ответе: напрямую из некоторых сетей поток
    обрывается ровно после 16 КБ, а короткие ответы проходят."""
    with _tlock:
        if _transport["kind"]:
            return _transport["kind"]
        order = (["proxy"] if http.detect_proxy() else []) + ["direct", "curl"]
        last = None
        for kind in order:
            try:
                m = _try(kind, f"{API}/api/v4/prematch/brand/{BRAND}/en/0", 6)
                vs = list(m.get("top_events_versions", [])) + list(m.get("rest_events_versions", []))
                if vs:
                    _try(kind, f"{API}/api/v4/prematch/brand/{BRAND}/en/{vs[0]}", 8)
                _transport["kind"] = kind
                return kind
            except Exception as e:
                last = e
        raise RuntimeError(f"линия BC Game недоступна: {last}")


def transport() -> str:
    return _transport["kind"] or "?"


def _get(mode: str, version, timeout: float = 15) -> dict:
    kind = _choose_transport()
    url = f"{API}/api/v4/{mode}/brand/{BRAND}/en/{version}"
    try:
        return _try(kind, url, timeout)
    except Exception:
        if kind == "curl":
            raise
        return _try(kind, url, timeout)     # соединение могло устареть — один повтор


class Feed:
    """Линия BC Game в одном режиме (прематч или лайв).

    step() — один запрос: первый вызов качает полный снимок, следующие — только
    изменения (и ждут их на сервере, если мы уже на самой свежей версии).
    run() — бесконечный цикл таких шагов для фонового потока; о каждом изменении
    сообщает через on_change(feed, {id: Event | None}).
    """

    def __init__(self, live: bool, on_change=None):
        self.live = live
        self.mode = "live" if live else "prematch"
        self.on_change = on_change
        self.version = None
        self.epoch = None
        self.raw: dict = {}
        self.sports: dict = {}
        self.cats: dict = {}
        self.tours: dict = {}
        self._parsed: dict = {}      # id события -> Event | False (пересобираем изменившиеся)
        self._family: dict = {}      # id вида спорта -> семейство | None
        self.generated = 0.0         # время сервера, на которое актуальна линия (unix, с)
        self.received = 0.0          # когда мы получили последний ответ
        self.error = None

    def _apply(self, part: dict) -> set:
        """Применить часть снимка или изменения. Возвращает id затронутых событий."""
        if part.get("sports"):
            self.sports.update(part["sports"])
            self._family.clear()
        self.cats.update(part.get("categories") or {})
        self.tours.update(part.get("tournaments") or {})
        touched = set()
        for eid, e in (part.get("events") or {}).items():
            touched.add(eid)
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
        return touched

    def _stamp(self, d: dict) -> None:
        self.received = time.time()
        gen = d.get("generated")
        if gen:
            self.generated = gen / 1000.0

    def _snapshot(self) -> set:
        old = set(self.raw)
        self.raw, self._parsed = {}, {}
        manifest = _get(self.mode, 0)
        self.epoch = manifest.get("epoch")
        versions = list(manifest.get("top_events_versions", [])) + \
            list(manifest.get("rest_events_versions", []))
        parts = list(POOL.map(lambda v: _get(self.mode, v), versions))
        for p in parts:
            self._apply(p)
        self.version = parts[-1].get("version") if parts else None
        # Снимок лежит в кэше CDN до 30 с, и его настоящий возраст неизвестен
        # (generated в манифесте — время манифеста, а не данных). Точное время
        # появится с первым же ответом с изменениями.
        self.generated = 0.0
        self.received = time.time()
        return old | set(self.raw)

    def step(self) -> set:
        """Один запрос. Возвращает id событий, которые могли измениться."""
        if self.version is None:
            return self._snapshot()
        d = _get(self.mode, self.version)
        if d.get("epoch") not in (None, self.epoch):
            return self._snapshot()             # сервер начал новую эпоху — всё заново
        touched = self._apply(d)
        self.version = d.get("version") or self.version
        self._stamp(d)
        return touched

    def sync(self) -> list[Event]:
        self.step()
        return self.events()

    def catch_up(self, max_steps: int = 120) -> None:
        """После снимка догнать текущий момент: снимок отстаёт на 10–30 с, а каждая
        версия — это одна секунда изменений."""
        for _ in range(max_steps):
            if time.time() - self.generated < 1.5:
                break
            self.step()

    def run(self) -> None:
        while True:
            try:
                touched = self.step()
                self.error = None
            except Exception as e:              # сеть нестабильна — не падаем
                self.error = e
                self.version = None             # после сбоя берём линию заново
                time.sleep(1.0)
                continue
            if touched and self.on_change:
                self.on_change(self, {eid: self.event(eid) for eid in touched})

    def event(self, eid: str) -> Event | None:
        e = self.raw.get(eid)
        if e is None:
            return None
        ev = self._parsed.get(eid)
        if ev is None:
            ev = self._parsed[eid] = self._build(eid, e) or False
        return ev or None

    def events(self) -> list[Event]:
        return [ev for ev in map(self.event, list(self.raw)) if ev]

    def family(self, sport_id: str) -> str | None:
        if sport_id not in self._family:
            fam = SPORTS.get(sport_id)
            if fam is None:
                fam = esport_family(self.sports.get(sport_id, {}).get("name", ""))
            self._family[sport_id] = fam
        return self._family[sport_id]

    def _build(self, eid: str, e: dict) -> Event | None:
        d = e.get("desc") or {}
        comps = d.get("competitors") or []
        if d.get("type") != "match" or len(comps) != 2 or d.get("virtual"):
            return None
        sport = self.family(str(d.get("sport")))
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
            live=self.live, info=clock if self.live else "", mode=self.mode,
            odds=odds,
        )


def fetch_events(live: bool = False) -> list[Event]:
    """Разовый полный снимок линии."""
    return Feed(live).sync()
