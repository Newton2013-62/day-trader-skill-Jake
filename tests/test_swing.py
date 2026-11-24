import datetime
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

swing = load("trader-swing", "swing")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
schwab = load("trader-broker", "schwab")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
NOW = "2026-10-19 10:15"
TODAY = datetime.date(2026, 10, 19)


def chart(n=300, seed=7, start=100.0, drift=0.0005, vol=0.018):
    """A Yahoo-shaped daily chart: a seeded random walk on trading days ending 2026-10-16."""
    rng = random.Random(seed)
    days = []
    d = datetime.date(2026, 10, 16)
    while len(days) < n:
        if swing.trading_day(d):
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.reverse()
    ts, o, h, l, c = [], [], [], [], []
    px = start
    for day in days:
        op = px * (1 + rng.gauss(0, vol / 3))
        cl = op * (1 + rng.gauss(drift, vol))
        hi = max(op, cl) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(op, cl) * (1 - abs(rng.gauss(0, vol / 2)))
        t = int((datetime.datetime.combine(day, datetime.time(13, 30)) - datetime.datetime(1970, 1, 1)).total_seconds())
        ts.append(t)
        o.append(op), h.append(hi), l.append(lo), c.append(cl)
        px = cl
    return {"chart": {"result": [{"meta": {"gmtoffset": -14400}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c}]}}]}}


def bars(**kw):
    return swing.parse_daily(chart(**kw))


class Plan(unittest.TestCase):
    def test_parse_and_atr(self):
        b = bars()
        self.assertEqual(len(b), 300)
        self.assertEqual(b[-1]["date"], "2026-10-16")
        a = swing.atr(b)
        self.assertGreater(a, 0)
        self.assertLess(a, b[-1]["close"] * 0.1)

    def test_default_plan_is_2_atr_2r_15_days(self):
        b = bars()
        p = swing.make_plan("abc", b, {}, today=TODAY)
        a = swing.atr(b)
        self.assertEqual(p["style"], "swing")
        self.assertEqual(p["symbol"], "ABC")
        self.assertEqual(p["entry"], swing.cent(b[-1]["close"]))
        self.assertAlmostEqual(p["stop"], swing.floor_cent(p["entry"] - 2 * a), places=2)
        self.assertAlmostEqual(p["target"], swing.cent(p["entry"] + 2 * p["risk_per_share"]), places=2)
        self.assertEqual(p["hold_days"], 15)
        self.assertIsNone(p["flat_by"])
        # 15 trading days after Mon 2026-10-19
        self.assertEqual(p["exit_by"], "2026-11-09")

    def test_the_time_limit_skips_thanksgiving(self):
        self.assertEqual(swing.add_trading_days(datetime.date(2026, 11, 24), 2), datetime.date(2026, 11, 27))

    def test_other_stops_and_targets(self):
        b = bars()
        p = swing.make_plan("ABC", b, {}, stop_rule="low10", target="1.5R", hold=10, today=TODAY)
        self.assertAlmostEqual(p["stop"], swing.floor_cent(min(x["low"] for x in b[-10:]) - 0.01), places=2)
        self.assertEqual(p["reward_risk"], 1.5)
        p = swing.make_plan("ABC", b, {}, entry=100, stop_rule="6%", target="120", today=TODAY)
        self.assertEqual((p["stop"], p["target"]), (94.0, 120.0))
        for bad in ({"stop_rule": "200"}, {"stop_rule": "bogus"}, {"target": "50"}, {"hold": 0}):
            with self.assertRaises(swing.SwingError, msg=bad):
                swing.make_plan("ABC", b, {}, entry=100, today=TODAY, **bad)

    def test_config_defaults_are_used(self):
        p = swing.make_plan("ABC", bars(), {"swing_hold_days": 5, "swing_target_r": 3}, today=TODAY)
        self.assertEqual((p["hold_days"], p["reward_risk"]), (5, 3.0))


class Earnings(unittest.TestCase):
    def test_estimate_from_past_filings(self):
        nxt, gap = swing.estimate_next(["2026-07-22", "2026-04-22", "2026-01-28", "2025-10-22"])
        self.assertEqual(gap, 91)
        self.assertEqual(nxt.isoformat(), "2026-10-21")

    def test_a_report_inside_the_hold_is_flagged(self):
        def fetch(url):
            if "company_tickers" in url:
                return {"0": {"ticker": "ABC", "cik_str": 123}}
            return {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K", "8-K"],
                                           "items": ["2.02,9.01", "", "5.02", "2.02"],
                                           "filingDate": ["2026-07-22", "2026-07-30", "2026-06-01", "2026-04-22"]}}}
        e = swing.earnings_check("ABC", TODAY, swing.add_trading_days(TODAY, 15), ua="me x@y.z", fetch=fetch)
        self.assertTrue(e["inside"])
        self.assertEqual(e["date"], "2026-10-21")
        p = swing.make_plan("ABC", bars(), {}, today=TODAY, earnings=e)
        self.assertTrue(any("EARNINGS" in w for w in p["warnings"]))

    def test_your_date_wins_and_no_user_agent_says_unknown(self):
        e = swing.earnings_check("ABC", TODAY, swing.add_trading_days(TODAY, 15), override="2026-12-15")
        self.assertFalse(e["inside"])
        e = swing.earnings_check("ABC", TODAY, swing.add_trading_days(TODAY, 15))
        self.assertIsNone(e["inside"])
        self.assertIn("unknown", e["note"])


class Replay(unittest.TestCase):
    def test_a_gap_through_the_stop_fills_at_the_open(self):
        self.assertEqual(swing._walk(100, 95, 110, [(101, 102, 99, 100), (90, 92, 89, 91)]), ("stop", 90, 2))
        self.assertEqual(swing._walk(100, 95, 110, [(101, 111, 94, 100)]), ("stop", 95, 1))
        self.assertEqual(swing._walk(100, 95, 110, [(101, 111, 99, 108)]), ("target", 110, 1))
        self.assertEqual(swing._walk(100, 95, 110, [(101, 103, 99, 102)]), ("time", 102, 1))

    def test_replay_adds_up_and_repeats_with_the_seed(self):
        b = bars()
        p = swing.make_plan("ABC", b, {}, today=TODAY)
        r1 = swing.replay(p, b, n=2000, seed=3)
        r2 = swing.replay(p, b, n=2000, seed=3)
        self.assertEqual(r1, r2)
        s = r1["simulated"]
        self.assertAlmostEqual(s["target"] + s["stop"] + s["time"], 100.0, delta=0.2)
        h = r1["history"]
        self.assertEqual(h["target"] + h["stop"] + h["time"], h["days"])
        self.assertEqual(h["days"], len(b) - 1 - p["hold_days"])
        self.assertTrue(r1["verdict"].startswith(("negative", "close to a coin flip", "positive on average")))

    def test_a_steady_downtrend_is_negative(self):
        b = bars(drift=-0.004, vol=0.01)
        p = swing.make_plan("ABC", b, {}, today=TODAY)
        r = swing.replay(p, b, n=2000)
        self.assertTrue(r["verdict"].startswith("negative"))


class Pipeline(unittest.TestCase):
    """plan -> budget -> risk -> Schwab JSON, with the swing rules."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(CFG_PATH) as fh:
            self.cfg = json.load(fh)
        self.plan = swing.make_plan("ABC", bars(start=40.0), self.cfg, today=TODAY)

    def tearDown(self):
        shutil.rmtree(self.d)

    def order(self, **cfg):
        c = dict(self.cfg, **cfg)
        s = size.size_trade(self.plan, c, today=TODAY.isoformat())
        self.assertIsNone(s["refusal"], s["refusal"])
        return size.bracket_order(s)

    def risk(self, orders, portfolio=None, state=None, **cfg):
        c = dict(self.cfg, **cfg)
        res = check.check(orders, c, portfolio=portfolio, prices={}, state=state,
                                 now=check.parse_now(NOW))
        return res["results"]

    def test_budget_writes_a_gtc_bracket(self):
        o = self.order()
        self.assertEqual(o["time_in_force"], "gtc")
        self.assertEqual(o["order_class"], "bracket")
        self.assertEqual(o["plan"]["style"], "swing")
        self.assertEqual(o["plan"]["exit_by"], self.plan["exit_by"])
        self.assertIsNone(o["plan"]["flat_by"])
        self.assertIn("swing trade", o["why"])

    def test_risk_passes_a_gtc_swing_and_blocks_a_day_one(self):
        o = self.order()
        r = self.risk([o])[0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["style"], "swing")
        day = dict(o, time_in_force="day")
        r = self.risk([day])[0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("swing:") for x in r["reasons"]))

    def test_open_positions_limit(self):
        o = self.order()
        pf = {"cash": 5000, "positions": [{"ticker": t, "shares": 1, "lots": []} for t in ("AAA", "BBB", "CCC")]}
        r = self.risk([o], portfolio=pf, max_order_usd=100000)[0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("open positions") for x in r["reasons"]), r["reasons"])

    def test_open_risk_limit_counts_held_swing_entries(self):
        o = self.order()
        pf = {"cash": 5000, "positions": [{"ticker": "AAA", "shares": 5, "lots": []},
                                          {"ticker": "BBB", "shares": 5, "lots": []}]}
        state = {"orders": [
            {"status": "placed", "date": "2026-10-09", "symbol": "AAA", "side": "buy", "kind": "entry",
             "style": "swing", "max_loss": 50},
            {"status": "placed", "date": "2026-10-12", "symbol": "BBB", "side": "buy", "kind": "entry",
             "style": "swing", "max_loss": 50},
            {"status": "placed", "date": "2026-10-13", "symbol": "ZZZ", "side": "buy", "kind": "entry",
             "style": "swing", "max_loss": 50}]}   # ZZZ was sold: not held, not counted
        r = self.risk([o], portfolio=pf, state=state)[0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        r = self.risk([o], portfolio=pf, state=state, max_open_risk_usd=120)[0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("open risk") for x in r["reasons"]), r["reasons"])

    def test_schwab_gets_a_day_entry_with_gtc_legs(self):
        body = schwab.to_schwab(self.order())
        self.assertEqual((body["orderType"], body["duration"]), ("MARKET", "DAY"))
        legs = body["childOrderStrategies"][0]["childOrderStrategies"]
        self.assertEqual({l["duration"] for l in legs}, {"GOOD_TILL_CANCEL"})

    def test_alerts_watch_every_day_and_ring_on_the_time_limit(self):
        rules = alerts.rules_from_plan(self.plan)
        self.assertEqual([r["type"] for r in rules], ["stop", "target", "time"])
        self.assertNotIn("date", rules[0])
        self.assertNotIn("date", rules[1])
        self.assertEqual(rules[2]["date"], self.plan["exit_by"])


class Cli(unittest.TestCase):
    def test_plan_and_replay_offline(self):
        d = tempfile.mkdtemp()
        try:
            ch = os.path.join(d, "abc.json")
            with open(ch, "w") as fh:
                json.dump(chart(), fh)
            plan = os.path.join(d, "trader", "plan.json")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = swing.main(["plan", "ABC", "--offline", ch, "--today", "2026-10-19", "--write", "--out", plan,
                                   "--config", os.path.join(d, "none.json"), "--earnings", "2026-10-28"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("SWING PLAN ABC", text)
            self.assertIn("EARNINGS likely inside", text)
            self.assertIn("not financial advice", text)
            out = io.StringIO()
            with redirect_stdout(out):
                code = swing.main(["replay", "--plan", plan, "--offline", ch, "--runs", "500"])
            self.assertEqual(code, 0)
            self.assertIn("TIME MACHINE (multi-day) ABC", out.getvalue())
            self.assertIn("Earnings likely inside", out.getvalue())
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
