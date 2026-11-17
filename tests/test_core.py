"""Проверки без сети: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb import bcgame, polymarket
from arb.calc import bc_complements, find_arb, pm_cost
from arb.match import WEAK_SCORE, flip_key, match_events
from arb.model import Event
from arb.sports import esport_family, is_esport


def ev(source, id, sport, c1, c2, start=1000, league=""):
    return Event(source=source, id=id, sport=sport, league=league, c1=c1, c2=c2, start=start)


class Sports(unittest.TestCase):
    def test_esport_names(self):
        self.assertEqual(esport_family("Counter-Strike"), "cs2")
        self.assertEqual(esport_family("dota-2"), "dota2")
        self.assertEqual(esport_family("Dota 2"), "dota2")
        self.assertEqual(esport_family("LoL"), "lol")
        self.assertEqual(esport_family("King of Glory"), "hok")
        self.assertEqual(esport_family("Honor of Kings"), "hok")
        self.assertIsNone(esport_family("Valencia"))
        self.assertIsNone(esport_family("Soccer"))
        self.assertTrue(is_esport("cs2"))
        self.assertFalse(is_esport("soccer"))


class Calc(unittest.TestCase):
    def test_complements(self):
        self.assertEqual(bc_complements(("ml", "c1")), [[("ml", "c2")]])
        self.assertEqual(bc_complements(("map", 2, "c2")), [[("map", 2, "c1")]])
        self.assertEqual(bc_complements(("total", 2.5, "over")), [[("total", 2.5, "under")]])
        self.assertEqual(bc_complements(("hcp", -1.5, "c1")), [[("hcp", -1.5, "c2")]])
        self.assertEqual(bc_complements(("btts", "yes")), [[("btts", "no")]])
        # «Да» на победу c1 закрывается X2: двойным шансом, парой исходов или форой c2 +0.5
        opts = bc_complements(("1x2", "c1"))
        self.assertIn([("dc", "xc2")], opts)
        self.assertIn([("1x2", "draw"), ("1x2", "c2")], opts)
        self.assertIn([("hcp", -0.5, "c2")], opts)
        self.assertIn([("hcp", -0.5, "c1")], bc_complements(("not1x2", "c1")))

    def test_find_arb(self):
        # BC кф 2.2 (inv 0.4545), PM 0.50 с комиссией 0.5125 -> сумма 0.967
        arb = find_arb(1 / 2.2, [(0.50, 100), (0.53, 50), (0.60, 100)], 0.05)
        self.assertIsNotNone(arb)
        self.assertEqual(arb.shares, 150)           # третий уровень уже убыточен
        self.assertGreater(arb.profit, 0)
        self.assertIsNone(find_arb(1 / 1.9, [(0.50, 100)], 0.05))
        self.assertAlmostEqual(pm_cost(0.5, 0.05), 0.5125)


class Match(unittest.TestCase):
    def pair(self, sport, bc, pm, dt=0, bc_league="", pm_league=""):
        b = ev("bc", "b", sport, *bc, start=1000, league=bc_league)
        p = ev("pm", "p", sport, *pm, start=1000 + dt, league=pm_league)
        return match_events([b], [p])

    def test_basic(self):
        m = self.pair("soccer", ("Man Utd", "Chelsea FC"), ("Chelsea", "Manchester United"))
        self.assertEqual(len(m), 1)
        self.assertTrue(m[0][2])                    # порядок команд перевёрнут

    def test_esports(self):
        self.assertEqual(len(self.pair("cs2", ("Team Spirit", "NAVI"), ("Spirit", "NaVi"), dt=4 * 3600)), 1)
        self.assertEqual(self.pair("cs2", ("Team Spirit Academy", "NAVI"), ("Spirit", "NaVi")), [])

    def test_doubles_initials(self):
        m = self.pair("tennis", ("Ellis B / Tu L", "Ayeni A / Isomura K"),
                      ("Ellis/Tu", "Ayeni/Isomura"), dt=3600)
        self.assertEqual(len(m), 1)

    def test_women(self):
        self.assertEqual(self.pair("soccer", ("Arsenal", "Everton Women"), ("Arsenal", "Everton")), [])
        m = self.pair("soccer", ("Tottenham Hotspur FC", "Everton Women"),
                      ("Tottenham Hotspur FC", "Everton FC"), pm_league="wsl-games")
        self.assertEqual(len(m), 1)
        m = self.pair("cricket", ("Namibia", "USA"), ("Namibia Women", "USA Women"),
                      bc_league="International / Women's T20")
        self.assertEqual(len(m), 1)

    def test_weak(self):
        m = self.pair("basketball", ("UConn Huskies", "Ohio State Buckeyes"),
                      ("Ohio State Buckeyes", "Storrs Huskies"))
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0][3], WEAK_SCORE)
        # у второй команды нет общего слова — не сопоставляем
        self.assertEqual(self.pair("soccer", ("Bucaneros", "Estudiantes de Queretaro"),
                                   ("CA Atlanta", "CA Estudiantes")), [])
        # время не совпало точно — не сопоставляем
        self.assertEqual(self.pair("basketball", ("UConn Huskies", "Ohio State Buckeyes"),
                                   ("Ohio State Buckeyes", "Storrs Huskies"), dt=1800), [])

    def test_tabletennis_time(self):
        self.assertEqual(self.pair("tabletennis", ("Ivanov R", "Chuhai S"),
                                   ("Ivanov Ruslan", "Chuhai Serhii"), dt=2 * 3600), [])

    def test_flip(self):
        self.assertEqual(flip_key(("hcp", -1.5, "c1")), ("hcp", 1.5, "c2"))
        self.assertEqual(flip_key(("map", 2, "c1")), ("map", 2, "c2"))
        self.assertEqual(flip_key(("total", 2.5, "over")), ("total", 2.5, "over"))


class BcParse(unittest.TestCase):
    def test_esport_markets(self):
        markets = {
            "186": {"": {"4": {"k": "1.16"}, "5": {"k": "4.9"}}},
            "330": {"mapnr=1": {"4": {"k": "1.3"}, "5": {"k": "3.4", "b": 1}}},
            "328": {"total=2.5": {"12": {"k": "2.4"}, "13": {"k": "1.55"}}},
            "327": {"hcp=-1.5": {"1714": {"k": "1.73"}, "1715": {"k": "2.06"}},
                    "hcp=1": {"1714": {"k": "1.2"}, "1715": {"k": "4.0"}}},
        }
        odds = bcgame._parse_markets("cs2", markets)
        self.assertEqual(odds[("ml", "c1")], 1.16)
        self.assertEqual(odds[("map", 1, "c1")], 1.3)
        self.assertNotIn(("map", 1, "c2"), odds)            # заблокированный исход
        self.assertEqual(odds[("total", 2.5, "under")], 1.55)
        self.assertEqual(odds[("hcp", -1.5, "c2")], 2.06)
        self.assertFalse(any(k[0] == "hcp" and k[1] == 1 for k in odds))   # целая линия

    def test_feed_family_by_name(self):
        f = bcgame.Feed(False)
        f.sports = {"111": {"name": "Dota 2"}, "300": {"name": "eSoccer"}, "1": {"name": "Soccer"}}
        self.assertEqual(f.family("111"), "dota2")
        self.assertIsNone(f.family("300"))
        self.assertEqual(f.family("1"), "soccer")


def market(kind, question, outcomes, tokens, line=None, title=None):
    import json
    return {"sportsMarketType": kind, "question": question, "groupItemTitle": title,
            "outcomes": json.dumps(outcomes), "clobTokenIds": json.dumps(tokens),
            "acceptingOrders": True, "line": line, "bestBid": 0.4, "bestAsk": 0.45}


class PmParse(unittest.TestCase):
    def test_esport_event(self):
        title = "Counter-Strike: LUNTIKI vs 33 (BO3) - PGL Cluj-Napoca: Open Qualifier 2 Playoffs"
        raw = [{
            "id": 1, "slug": "cs2-lun-331-2026-11-13", "title": title,
            "startTime": "2026-11-13T15:00:00Z",
            "tags": [{"slug": "esports"}, {"slug": "counter-strike-2"}],
            "markets": [
                market("moneyline", title, ["LUNTIKI", "33"], ["a", "b"], title="Match Winner"),
                market("child_moneyline", "Counter-Strike: LUNTIKI vs 33 - Map 2 Winner",
                       ["LUNTIKI", "33"], ["c", "d"], title="Map 2 Winner"),
                market("totals", "Games Total: O/U 2.5", ["Over", "Under"], ["e", "f"], line=2.5),
                market("map_handicap", "Map Handicap: 33 (-1.5) vs LUNTIKI (+1.5)",
                       ["33", "LUNTIKI"], ["g", "h"], line=-1.5),
            ]}]
        evs = polymarket.parse_events(raw, hours=48, live=None, now=1794582000)
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e.sport, e.c1, e.c2), ("cs2", "LUNTIKI", "33"))
        self.assertEqual(e.sels[("ml", "c2")].token, "b")
        self.assertEqual(e.sels[("map", 2, "c1")].token, "c")
        self.assertEqual(e.sels[("total", 2.5, "under")].token, "f")
        # фора -1.5 у «33» (c2) = фора c1 +1.5
        self.assertEqual(e.sels[("hcp", 1.5, "c2")].token, "g")
        self.assertEqual(e.sels[("hcp", 1.5, "c1")].token, "h")

    def test_title_cleanup(self):
        self.assertEqual(polymarket._teams_from_title("Dota 2: Team Liquid vs LGD Gaming (BO3) - 1win Essence"),
                         ("Team Liquid", "LGD Gaming"))


class WholeStakes(unittest.TestCase):
    def test_round(self):
        from arb.view import whole
        self.assertEqual([whole(x) for x in (52.49, 52.5, 0.2, 7.0)], [52, 53, 1, 7])


class Ws(unittest.TestCase):
    def test_frames(self):
        from arb.ws import WebSocket
        ws = WebSocket.__new__(WebSocket)
        ws._parts = []
        payload = b"x" * 300
        frame = bytes([0x81, 126]) + (300).to_bytes(2, "big") + payload
        ws._buf = frame[:100]
        self.assertIsNone(ws._frame())              # кадр пришёл не целиком
        self.assertEqual(len(ws._buf), 100)         # и буфер не тронут
        ws._buf = frame + bytes([0x81, 2]) + b"ok"
        self.assertEqual(ws._frame()[2], payload)
        self.assertEqual(ws._frame()[2], b"ok")


if __name__ == "__main__":
    unittest.main()
