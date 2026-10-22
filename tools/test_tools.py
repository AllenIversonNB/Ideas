#!/usr/bin/env python3
"""python3 tools/test_tools.py  -- merge identity rules, performance math, yfinance frame parsing."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import merge_ideas as M  # noqa: E402
import update_prices as P  # noqa: E402

EV = {"key": "e1", "d": "2026-10-19", "name": "MCH TMT Idea Dinner", "host": "Monness, Crespi, Hardt",
      "series": "MCH Idea Dinner", "fmt": "Dinner", "src": "a | s1 | 2026-10-21"}


def idea(**kw):
    base = {"ev_key": "e1", "d": "2026-10-19", "tk": "IMAX", "yf": "IMAX", "co": "IMAX Corp.", "dir": "Short",
            "pr": "Undisclosed", "th": "Odyssey year is not repeatable", "pitch": "short text", "src": "a | s1 | 2026-10-21"}
    base.update(kw)
    return base


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.store = {"updated": None, "events": [], "ideas": []}

    def add(self, events, ideas, now="2026-10-22T09:00:00-04:00"):
        st = M.merge(self.store, {"events": events, "ideas": ideas}, now)
        self.assertEqual(M.validate(self.store), [])
        return st

    def test_new_then_idempotent(self):
        self.add([EV], [idea(), idea(tk="FLEX", yf="FLEX", co="Flex", dir="Long")])
        self.assertEqual(len(self.store["ideas"]), 2)
        st = self.add([EV], [idea(), idea(tk="FLEX", yf="FLEX", co="Flex", dir="Long")])
        self.assertEqual(len(self.store["ideas"]), 2)
        self.assertEqual((st["new"], st["upd"]), (0, 0))
        ids = {i["id"] for i in self.store["ideas"]}
        self.assertIn("2026-10-19-monness-crespi-mch-tmt-idea-dinner--imax-s", ids)

    def test_longer_pitch_wins_and_src_unions(self):
        self.add([EV], [idea()])
        self.add([dict(EV, src="b | s2 | 2026-10-22")], [idea(pitch="a much longer pitch text here", src="b | s2 | 2026-10-22")])
        i = self.store["ideas"][0]
        self.assertEqual(i["pitch"], "a much longer pitch text here")
        self.assertEqual(i["src"], "a | s1 | 2026-10-21 || b | s2 | 2026-10-22")
        self.assertEqual(self.store["events"][0]["src"], "a | s1 | 2026-10-21 || b | s2 | 2026-10-22")
        self.add([EV], [idea(pr="Zack Jersky", fm="Some Capital")])
        self.assertEqual((self.store["ideas"][0]["pr"], self.store["ideas"][0]["fm"]), ("Zack Jersky", "Some Capital"))
        self.add([EV], [idea(pr="Undisclosed")])
        self.assertEqual(self.store["ideas"][0]["pr"], "Zack Jersky")

    def test_correction_flips_direction_and_keeps_alias(self):
        self.add([EV], [idea(tk="EBAY", yf="EBAY", co="eBay", dir="Long")])
        old = self.store["ideas"][0]["id"]
        st = self.add([EV], [idea(tk="EBAY", yf="EBAY", co="eBay", dir="Short", correct=True)])
        self.assertEqual(st["corrected"], 1)
        self.assertEqual(len(self.store["ideas"]), 1)
        i = self.store["ideas"][0]
        self.assertEqual(i["dir"], "Short")
        self.assertIn(old, i["aka"])
        self.assertTrue(i["id"].endswith("--ebay-s"))

    def test_ext_match_moves_to_earlier_date(self):
        late = {"key": "x", "d": "2026-08-01", "name": "DeMatteo Investor Idea: VICR Long", "host": "DeMatteo Research",
                "series": "DeMatteo Investor Idea", "fmt": "Call"}
        early = dict(late, d="2026-06-01")
        self.add([late], [idea(ev_key="x", d="2026-08-01", tk="VICR", yf="VICR", co="Vicor", dir="Long", ext="TOK1")])
        self.add([early], [idea(ev_key="x", d="2026-06-01", tk="VICR", yf="VICR", co="Vicor", dir="Long", ext="TOK1")])
        self.assertEqual(len(self.store["ideas"]), 1)
        i = self.store["ideas"][0]
        self.assertEqual(i["d"], "2026-06-01")
        self.assertTrue(i["ev"].startswith("2026-06-01"))
        self.assertEqual([e["d"] for e in self.store["events"]], ["2026-06-01"])

    def test_relisted_idea_leaves_no_empty_event(self):
        ev = {"key": "x", "d": "2026-08-01", "name": "DeMatteo Investor Idea: VICR Long", "host": "DeMatteo Research",
              "series": "DeMatteo Investor Idea", "fmt": "Call"}
        self.add([ev], [idea(ev_key="x", d="2026-08-01", tk="VICR", yf="VICR", co="Vicor", dir="Long", ext="TOK1")])
        st = self.add([dict(ev, d="2026-08-05")], [idea(ev_key="x", d="2026-08-05", tk="VICR", yf="VICR", co="Vicor", dir="Long", ext="TOK1")])
        self.assertEqual(len(self.store["events"]), 1)
        self.assertEqual(st["ev_new"], 0)

    def test_two_tokens_same_pitch_fold(self):
        mk = lambda d: {"key": "x", "d": d, "name": "DeMatteo Investor Idea: UPS Short", "host": "DeMatteo Research",
                        "series": "DeMatteo Investor Idea", "fmt": "Call"}
        self.add([mk("2026-07-27")], [idea(ev_key="x", d="2026-07-27", tk="UPS", yf="UPS", co="UPS", ext="B")])
        st = self.add([mk("2026-05-31")], [idea(ev_key="x", d="2026-05-31", tk="UPS", yf="UPS", co="UPS", ext="A"),
                                           idea(ev_key="x", d="2026-05-31", tk="UPS", yf="UPS", co="UPS", ext="B")])
        self.assertEqual(len(self.store["ideas"]), 1)
        self.assertEqual(st["folded"], 1)
        i = self.store["ideas"][0]
        self.assertEqual(i["d"], "2026-05-31")
        self.assertIn("B", i["aka"])
        self.assertEqual([e["d"] for e in self.store["events"]], ["2026-05-31"])
        st = self.add([mk("2026-08-02")], [idea(ev_key="x", d="2026-08-02", tk="UPS", yf="UPS", co="UPS", ext="B")])
        self.assertEqual((st["new"], len(self.store["ideas"])), (0, 1))

    def test_catchall_copy_folds_into_named_event(self):
        din = {"key": "din", "d": "2026-06-09", "name": "DeMatteo TMT Dinner", "host": "DeMatteo Research",
               "series": "DeMatteo TMT Dinner", "fmt": "Dinner"}
        one = {"key": "one", "d": "2026-06-10", "name": "DeMatteo Investor Idea: MRVL Long", "host": "DeMatteo Research",
               "series": "DeMatteo Investor Idea", "fmt": "Call"}
        self.add([one], [idea(ev_key="one", d="2026-06-10", tk="MRVL", yf="MRVL", co="Marvell", dir="Long", ext="T2", pitch="longer text from the table")])
        st = self.add([din], [idea(ev_key="din", d="2026-06-09", tk="MRVL", yf="MRVL", co="Marvell", dir="Long", ext="T1")])
        self.assertEqual(st["folded"], 1)
        self.assertEqual(len(self.store["ideas"]), 1)
        i = self.store["ideas"][0]
        self.assertTrue(i["ev"].endswith("dematteo-tmt-dinner"))
        self.assertIn("T2", i["aka"])
        self.assertEqual(i["pitch"], "longer text from the table")
        self.assertEqual(len(self.store["events"]), 1)
        # A different host's pitch of the same name is a separate idea.
        mch = dict(EV, key="m", d="2026-06-09")
        self.add([mch], [idea(ev_key="m", d="2026-06-09", tk="MRVL", yf="MRVL", co="Marvell", dir="Long")])
        self.assertEqual(len(self.store["ideas"]), 2)

    def test_same_ticker_both_directions_are_two_ideas(self):
        self.add([EV], [idea(tk="META", dir="Long"), idea(tk="META", dir="Short")])
        self.assertEqual(len(self.store["ideas"]), 2)

    def test_bad_input_refused(self):
        with self.assertRaises(SystemExit):
            M.merge(self.store, {"events": [EV], "ideas": [idea(dir="Neutral")]}, "now")
        with self.assertRaises(SystemExit):
            M.merge(self.store, {"events": [EV], "ideas": [idea(ev_key="nope")]}, "now")

    def test_write_splits_pitch_text(self):
        self.add([dict(EV, notes="event notes")], [idea()])
        with tempfile.TemporaryDirectory() as td:
            M.STORE, M.PUB, M.PITCHES = (os.path.join(td, n) for n in ("s.json", "p.json", "t.json"))
            M.write_all(self.store, "now")
            pub = json.load(open(M.PUB, encoding="utf-8"))  # noqa: SIM115
            pit = json.load(open(M.PITCHES, encoding="utf-8"))  # noqa: SIM115
            self.assertNotIn("pitch", pub["ideas"][0])
            self.assertEqual(pub["events"][0]["has_notes"], 1)
            self.assertEqual(pit["ideas"][pub["ideas"][0]["id"]], "short text")
            self.assertEqual(pit["events"][pub["events"][0]["id"]], "event notes")


def days(start, n, px0=100.0, step=1.0):
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    out, k, p = [], 0, px0
    while len(out) < n:
        d = d0 + dt.timedelta(days=k)
        k += 1
        if d.weekday() < 5:
            out.append((d.isoformat(), p, p))
            p += step
    return out


class PerfTest(unittest.TestCase):
    def test_long_short_alpha_and_windows(self):
        s = P.Series(days("2026-01-01", 120, 100, 1))     # rising stock
        b = P.Series(days("2026-01-01", 120, 100, 0.5))   # rising slower
        lng = P.perf({"d": "2026-01-03", "dir": "Long"}, s, b)   # Saturday -> uses Fri 01-02 close
        self.assertEqual(lng["px0_d"], "2026-01-02")
        self.assertEqual(lng["px0"], 101.0)
        self.assertEqual(lng["d30"], "2026-02-02")               # 02-02 is a Monday on/after 02-02
        self.assertGreater(lng["p"], 0)
        self.assertGreater(lng["a"], 0)
        sht = P.perf({"d": "2026-01-03", "dir": "Short"}, s, b)
        self.assertAlmostEqual(sht["p"], -lng["p"], places=6)
        self.assertAlmostEqual(sht["r"], lng["r"], places=6)
        self.assertAlmostEqual(sht["a"], -lng["a"], places=6)

    def test_pending_window(self):
        s = P.Series(days("2026-10-01", 15))
        r = P.perf({"d": "2026-10-10", "dir": "Long"}, s, s)
        self.assertNotIn("p30", r)
        self.assertEqual(r["d30"], "2026-11-09")
        self.assertNotIn("p90", r)

    def test_pre_ipo_pitch_starts_at_first_close(self):
        s = P.Series(days("2026-03-02", 10))
        r = P.perf({"d": "2026-02-15", "dir": "Long"}, s, None)
        self.assertEqual(r["px0_d"], "2026-03-02")
        self.assertNotIn("a", r)

    def test_mcap_usd(self):
        self.assertEqual(P.usd_mcap({"cur": "USD", "shares": 10}, 5.0, {}), 50.0)
        self.assertAlmostEqual(P.usd_mcap({"cur": "GBp", "shares": 100}, 250.0, {"GBP": 1.3}), 325.0)
        self.assertIsNone(P.usd_mcap({"cur": "KRW", "shares": 1}, 1.0, {}))
        self.assertEqual(P.usd_mcap({"cur": "EUR", "mcap_local": 1000}, None, {"EUR": 1.1}), 1100.0)

    def test_build_skips_unlisted(self):
        s = P.Series(days("2026-01-01", 5))
        out = P.build([{"id": "a", "yf": "", "d": "2026-01-02", "dir": "Long"},
                       {"id": "b", "yf": "X", "d": "2026-01-02", "dir": "Long"}],
                      {"X": s}, {"X": {"cur": "USD", "shares": 2}}, {}, s)
        self.assertEqual(list(out), ["b"])
        self.assertEqual(out["b"]["mcap"], round(2 * s.c[-1]))


class FrameTest(unittest.TestCase):
    """fetch_series must read yfinance's group_by='ticker' frame (multi- and single-symbol shapes)."""

    def test_parse(self):
        import pandas as pd
        idx = pd.to_datetime(["2026-01-02", "2026-01-05"])
        cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Open", "Close", "Adj Close"]])
        df = pd.DataFrame([[1, 10, 9, 1, 20, 19], [1, 11, 10, 1, float("nan"), float("nan")]], index=idx, columns=cols)
        single = pd.DataFrame({"Close": [5.0], "Adj Close": [4.5]}, index=pd.to_datetime(["2026-01-02"]))

        class FakeYF:
            @staticmethod
            def download(chunk, **kw):
                return df if len(chunk) > 1 else single

        got = P.fetch_series(FakeYF, ["AAA", "BBB", "CCC"], "2026-01-01")
        self.assertEqual(got["AAA"].d, ["2026-01-02", "2026-01-05"])
        self.assertEqual(got["AAA"].a, [9.0, 10.0])
        self.assertEqual(got["BBB"].c, [20.0])
        self.assertNotIn("CCC", got)
        one = P.fetch_series(FakeYF, ["ZZZ"], "2026-01-01")
        self.assertEqual(one["ZZZ"].c, [5.0])

        # Recent yfinance returns (symbol, field) columns even for a single symbol.
        multi1 = pd.DataFrame([[5.0, 4.5]], index=pd.to_datetime(["2026-01-02"]),
                              columns=pd.MultiIndex.from_product([["EURUSD=X"], ["Close", "Adj Close"]]))

        class FakeYF2:
            @staticmethod
            def download(chunk, **kw):
                return multi1

        got = P.fetch_series(FakeYF2, ["EURUSD=X"], "2026-01-01")
        self.assertEqual(got["EURUSD=X"].c, [5.0])
        self.assertEqual(P.fetch_series(FakeYF2, ["OTHER"], "2026-01-01"), {})


if __name__ == "__main__":
    unittest.main(verbosity=1)
