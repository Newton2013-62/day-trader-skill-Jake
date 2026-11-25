import datetime
import gzip
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

edge = load("trader-swing", "edge")
bt = load("trader-swing", "backtest")
journal = load("trader-swing", "journal")
scan = load("trader-scanner", "scan")


def trading_days(end, n):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(days))


def chart(n=600, seed=7, start=50.0, drift=0.0006, vol=0.02, end=datetime.date(2026, 11, 24), volume=2e6):
    """A Yahoo-shaped daily chart: a seeded random walk."""
    rng = random.Random(seed)
    ts, o, h, l, c, v = [], [], [], [], [], []
    px = start
    for day in trading_days(end, n):
        op = px * (1 + rng.gauss(0, vol / 3))
        cl = op * (1 + rng.gauss(drift, vol))
        hi = max(op, cl) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(op, cl) * (1 - abs(rng.gauss(0, vol / 2)))
        ts.append(int(datetime.datetime(day.year, day.month, day.day, 14, 30).timestamp()) + 18000 - 18000)
        o.append(op); h.append(hi); l.append(lo); c.append(cl); v.append(volume)
        px = cl
    return {"chart": {"result": [{"meta": {"gmtoffset": -18000}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c,
                                                            "volume": v}]}}]}}


def bars_from(closes, start=datetime.date(2024, 1, 1)):
    out = []
    for d, c in zip(trading_days(start + datetime.timedelta(days=len(closes) * 2), len(closes)), closes):
        out.append({"date": d.isoformat(), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6})
    return out


class Data(unittest.TestCase):
    def test_parse_bars_keeps_volume_and_drops_gaps(self):
        doc = chart(n=30)
        q = doc["chart"]["result"][0]["indicators"]["quote"][0]
        q["close"][5] = None
        q["volume"][6] = None
        bars = edge.parse_bars(doc)
        self.assertEqual(len(bars), 29)
        self.assertEqual(bars[5]["volume"], 0.0)
        self.assertIn("volume", bars[0])
        self.assertLess(bars[0]["date"], bars[-1]["date"])

    def test_parse_bars_refuses_too_few(self):
        with self.assertRaises(edge.EdgeError):
            edge.parse_bars(chart(n=10), min_bars=20)


class Regime(unittest.TestCase):
    def test_uptrend_is_risk_on(self):
        closes = [100 * 1.002 ** i for i in range(260)]
        st, nums = edge.regime_at(closes, len(closes) - 1)
        self.assertEqual(st, "risk-on")
        self.assertTrue(nums["sma50_rising"])

    def test_under_200_day_is_risk_off(self):
        closes = [100 * 1.002 ** i for i in range(200)] + [100 * 1.002 ** 200 * 0.99 ** i for i in range(1, 61)]
        st, _ = edge.regime_at(closes, len(closes) - 1)
        self.assertEqual(st, "risk-off")

    def test_pullback_above_200_is_caution(self):
        closes = [100 * 1.003 ** i for i in range(250)] + [100 * 1.003 ** 250 * 0.995 ** i for i in range(1, 15)]
        st, nums = edge.regime_at(closes, len(closes) - 1)
        self.assertEqual(st, "caution")
        self.assertGreater(nums["pct_vs_200"], 0)
        self.assertLess(nums["pct_vs_50"], 0)

    def test_too_little_data(self):
        self.assertEqual(edge.regime_at([1.0] * 100, 99), (None, {}))

    def test_series_skips_warmup(self):
        bars = bars_from([100 * 1.001 ** i for i in range(300)])
        ser = edge.regime_series(bars)
        self.assertNotIn(bars[150]["date"], ser)
        self.assertEqual(ser[bars[-1]["date"]], "risk-on")


class RelativeStrength(unittest.TestCase):
    def test_leader_scores_positive_and_laggard_negative(self):
        spy = [100 * 1.0005 ** i for i in range(200)]
        lead = [100 * 1.002 ** i for i in range(200)]
        lag = [100 * 0.999 ** i for i in range(200)]
        s1, p1 = edge.rs_score(lead, spy)
        s2, _ = edge.rs_score(lag, spy)
        self.assertGreater(s1, 0)
        self.assertLess(s2, 0)
        self.assertEqual(set(p1), {"21d", "63d", "126d"})

    def test_young_stock_uses_what_it_has(self):
        spy = [100.0 + i for i in range(200)]
        s, parts = edge.rs_score([10.0 + i for i in range(70)], spy)
        self.assertIsNotNone(s)
        self.assertNotIn("126d", parts)

    def test_rank_pct(self):
        r = edge.rank_pct({"A": -5, "B": 0, "C": 10, "D": None})
        self.assertEqual(r["A"], 1)
        self.assertEqual(r["C"], 99)
        self.assertNotIn("D", r)


class Backtest(unittest.TestCase):
    def test_setups_match_the_scanner(self):
        bars = edge.parse_bars(chart(n=400, seed=3, vol=0.03))
        closes = [b["close"] for b in bars]
        rsi = bt.rsi_series(closes)
        agree = total = 0
        for i in range(150, len(closes)):
            fast = set(bt.setups_at(closes, i, rsi))
            slow = set(scan.find_setups(closes[i - 119:i + 1])[0])
            # RSI is seeded differently, so only the rsi-based tags may differ, and rarely
            self.assertEqual({t for t in fast if "oversold" not in t}, {t for t in slow if "oversold" not in t})
            total += 1
            agree += fast == slow
        self.assertGreater(agree / total, 0.97)

    def test_walk(self):
        self.assertEqual(bt.walk(10, 9, 13, [(10, 10.5, 9.5, 10), (10, 13.2, 9.8, 13)])[:2], ("target", 13))
        self.assertEqual(bt.walk(10, 9, 13, [(10, 10.5, 9.5, 10), (8.5, 9, 8, 8.8)])[:2], ("stop", 8.5))   # gap
        self.assertEqual(bt.walk(10, 9, 13, [(10, 13.5, 8.9, 10)])[:2], ("stop", 9))                       # both: stop
        self.assertEqual(bt.walk(10, 9, 13, [(10, 10.5, 9.5, 10.2)] * 3), ("time", 10.2, 3))
        # the entry day's open is the entry, never a gap
        self.assertEqual(bt.walk(10, 9, 13, [(10, 10.2, 9.9, 10.1), (10.1, 10.2, 10, 10.1)])[0], "time")

    def rules(self, **kw):
        r = {"min_price": 3, "min_dollar_volume": 3e6, "max_atr_pct": 6, "hold": 30, "atr_mult": 2,
             "target_r": 3, "slippage": 0.001, "setups": list(bt.ALL_SETUPS)}
        r.update(kw)
        return r

    def test_signals_one_at_a_time_and_consistent(self):
        bars = edge.parse_bars(chart(n=700, seed=11, vol=0.02))
        spy = edge.parse_bars(chart(n=700, seed=1, vol=0.01))
        trades = bt.signals_for("XYZ", bars, {b["date"]: b["close"] for b in spy}, edge.regime_series(spy),
                                self.rules())
        self.assertGreater(len(trades), 5)
        for a, b in zip(trades, trades[1:]):
            self.assertGreater(b["entry_date"], a["exit_date"])
        for t in trades:
            self.assertAlmostEqual(t["target"] - t["entry"], 3 * (t["entry"] - t["stop"]), places=6)
            self.assertLessEqual(t["days"], 30)
            if t["outcome"] == "target":
                self.assertAlmostEqual(t["r"], 3, delta=0.1)
            if t["outcome"] == "stop":
                self.assertLessEqual(t["r"], -0.99)

    def test_liquidity_filter(self):
        bars = edge.parse_bars(chart(n=500, seed=11, volume=1000))
        spy = edge.parse_bars(chart(n=500, seed=1))
        self.assertEqual(bt.signals_for("THIN", bars, {b["date"]: b["close"] for b in spy}, {}, self.rules()), [])

    def test_above_ma_filter_and_ma_exit(self):
        bars = edge.parse_bars(chart(n=700, seed=11, vol=0.025))
        spy = edge.parse_bars(chart(n=700, seed=1, vol=0.01))
        sc, rg = {b["date"]: b["close"] for b in spy}, edge.regime_series(spy)
        closes = [b["close"] for b in bars]
        idx = {b["date"]: k for k, b in enumerate(bars)}
        base = self.rules(setups=["oversold", "oversold_bounce"], target_r=2, hold=15)
        filt = bt.signals_for("X", bars, sc, rg, dict(base, above_ma=200))
        for t in filt:
            i = idx[t["signal_date"]]
            self.assertGreater(closes[i], sum(closes[i - 199:i + 1]) / 200)
        ex = bt.signals_for("X", bars, sc, rg, dict(base, exit_ma=5))
        self.assertTrue(any(t["outcome"] == "bounce" for t in ex))
        for t in ex:
            if t["outcome"] == "bounce":
                j = idx[t["exit_date"]]
                self.assertGreater(closes[j], sum(closes[j - 4:j + 1]) / 5)
                self.assertLessEqual(t["days"], 15)

    def test_walk_ma(self):
        bars = [{"open": 10, "high": 10.2, "low": 9.8, "close": c} for c in (12, 11, 10, 9.9, 10.5)]
        closes = [b["close"] for b in bars]
        self.assertEqual(bt.walk_ma(10, 9, 13, bars, 3, 5, 3, closes), ("bounce", 10.5, 2))
        self.assertEqual(bt.walk_ma(10, 9, 13, bars, 3, 1, 3, closes), ("time", 9.9, 1))
        self.assertEqual(bt.walk_ma(10, 9, 13, bars, 4, 5, 3, closes)[:2], ("bounce", 10.5))

    def _flat(self, n=140, px=100.0, vol=1e6):
        return [{"date": "d%d" % k, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": vol}
                for k in range(n)]

    def test_lance_capitulation(self):
        bars = self._flat()
        # three accelerating down days, the last on 4x volume, then a bounce over the prior high
        for k, (c, v) in zip((133, 134, 135), ((95, 1e6), (88, 1.5e6), (75, 4e6))):
            bars[k] = {"date": "d%d" % k, "open": c + 2, "high": c + 3, "low": c - 1, "close": c, "volume": v}
        bars[136] = {"date": "d136", "open": 75, "high": 77, "low": 72, "close": 76, "volume": 2e6}
        bars[137] = {"date": "d137", "open": 76, "high": 80, "low": 75.5, "close": 79, "volume": 2e6}
        closes = [b["close"] for b in bars]
        got = bt.lance_setups(bars, closes, 137, 3.0, ["capitulation"])
        self.assertEqual(got, [("capitulation", 71.99)])
        # no turn yet on the flush day itself, and no setup without the volume spike
        self.assertEqual(bt.lance_setups(bars, closes, 136, 3.0, ["capitulation"]), [])
        bars[135]["volume"] = 1e6
        self.assertEqual(bt.lance_setups(bars, closes, 137, 3.0, ["capitulation"]), [])

    def test_lance_breakout_6m(self):
        bars = self._flat()
        bars[139] = {"date": "d139", "open": 100, "high": 106, "low": 99.5, "close": 105, "volume": 2e6}
        closes = [b["close"] for b in bars]
        self.assertEqual(bt.lance_setups(bars, closes, 139, 2.0, ["breakout_6m"]), [("breakout_6m", 99.49)])
        bars[139]["volume"] = 1.2e6           # not enough volume
        self.assertEqual(bt.lance_setups(bars, closes, 139, 2.0, ["breakout_6m"]), [])
        self.assertEqual(bt.lance_setups(bars[:100], closes[:100], 99, 2.0, ["breakout_6m"]), [])

    def test_walk_trail_low(self):
        rows = [(10, 10.5, 9.8, 10.2), (10.2, 11.5, 10.1, 11.2), (11.2, 12, 11.0, 11.8), (11.8, 11.9, 10.9, 11.0)]
        bars = [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in rows]
        closes = [b["close"] for b in bars]
        # 1R = 1: day 2 closes 11.2 >= 11, stop trails to 10.1, then 11.0; day 4 low 10.9 hits it
        self.assertEqual(bt.walk_trail(10, 9, None, bars, 0, 10, "low", closes, 1.0), ("trail", 11.0, 4))
        # before 1R the stop stays put
        self.assertEqual(bt.walk_trail(10, 9, None, bars[:1], 0, 10, "low", closes, 1.0), ("time", 10.2, 1))
        # a target still wins when it is set
        self.assertEqual(bt.walk_trail(10, 9, 11.4, bars, 0, 10, "low", closes, 1.0), ("target", 11.4, 2))

    def test_walk_trail_ma20(self):
        closes = [10.0] * 20 + [11, 12, 9.5]
        bars = [{"open": c, "high": c + 0.2, "low": c - 0.2, "close": c} for c in closes]
        self.assertEqual(bt.walk_trail(10.9, 8, None, bars, 20, 10, "ma20", closes, 2.9), ("ma20", 9.5, 3))

    def test_keep_variants(self):
        t = {"regime": "risk-on", "rs": 5.0}
        self.assertTrue(all(bt.keep(t, v) for v, _ in bt.VARIANTS))
        t = {"regime": "caution", "rs": -1}
        self.assertEqual([bt.keep(t, v) for v, _ in bt.VARIANTS], [True, False, False, False])

    def test_account_respects_limits(self):
        dates = [d.isoformat() for d in trading_days(datetime.date(2026, 3, 1), 40)]
        trades = []
        for k in range(8):   # eight signals on the same day: only four fit
            trades.append({"symbol": "S%d" % k, "entry_date": dates[1], "exit_date": dates[10], "entry": 20.0,
                           "stop": 19.0, "exit": 23.0, "rs": float(k), "r": 3.0, "outcome": "target", "days": 10})
        cfg = dict(bt.DEFAULTS, budget=3000, max_open_positions=4, max_loss_per_trade_usd=50,
                   max_open_risk_usd=200, max_order_usd=1000, max_position_pct=35, cash_buffer_pct=10)
        acct, curve = bt.account(trades, {}, dates, cfg)
        # $3,000 less the 10% buffer is $2,700 to spend: two $1,000 orders, then $700 for a third
        self.assertEqual(acct["taken"], 3)
        self.assertEqual([t["symbol"] for t in acct["trades"]], ["S7", "S6", "S5"])   # best RS first
        self.assertEqual([t["shares"] for t in acct["trades"]], [50, 50, 35])
        for t in acct["trades"]:
            self.assertLessEqual(t["risk_usd"], 50)
        self.assertAlmostEqual(acct["end"], 3000 + 135 * 3, places=2)

    def test_account_open_risk_cap(self):
        dates = [d.isoformat() for d in trading_days(datetime.date(2026, 3, 1), 40)]
        trades = [{"symbol": "S%d" % k, "entry_date": dates[1], "exit_date": dates[10], "entry": 5.0,
                   "stop": 4.0, "exit": 5.0, "rs": 1.0, "r": 0.0, "outcome": "time", "days": 10} for k in range(6)]
        cfg = dict(bt.DEFAULTS, budget=100000, max_order_usd=100000, max_position_pct=100,
                   max_open_positions=10, max_open_risk_usd=120)
        acct, _ = bt.account(trades, {}, dates, cfg)
        self.assertEqual([t["shares"] for t in acct["trades"]], [50, 50, 20])

    def test_end_to_end_offline(self):
        tmp = tempfile.mkdtemp()
        try:
            cache = os.path.join(tmp, "cache")
            os.makedirs(cache)
            for sym, seed in (("SPY", 1), ("AAA", 2), ("BBB", 3), ("CCC", 4)):
                with gzip.open(os.path.join(cache, "%s_10y.json.gz" % sym), "wt") as fh:
                    json.dump(chart(n=1600, seed=seed, vol=0.012 if sym == "SPY" else 0.022), fh)
            out = os.path.join(tmp, "bt.json")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = bt.main(["--symbols", "AAA,BBB,CCC", "--cache", cache, "--config", os.path.join(tmp, "none.json"),
                              "--out", out, "--pause", "0", "--cache-hours", "1e9"])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            for s in ("SWING BACKTEST", "AS YOUR ACCOUNT", "just holding SPY", "Verdict:", "delisted",
                      "not financial advice"):
                self.assertIn(s, text)
            with open(out) as fh:
                res = json.load(fh)
            self.assertEqual(set(res["variants"]), {"all", "regime", "rs", "both"})
            self.assertGreater(res["variants"]["all"]["every_trade"]["trades"],
                               res["variants"]["both"]["every_trade"]["trades"])
        finally:
            shutil.rmtree(tmp)


class Journal(unittest.TestCase):
    def plan(self, **kw):
        p = {"symbol": "sofi", "entry": 14.2, "stop": 13.1, "target": 17.5, "hold_days": 5, "setup": "bull_flag",
             "date": "2026-11-02"}
        p.update(kw)
        return p

    def test_add_validates_and_numbers(self):
        doc = {"entries": []}
        e = journal.new_entry(doc, self.plan(), "suggested", "risk-on", 12.0)
        self.assertEqual((e["id"], e["symbol"], e["regime"]), (1, "SOFI", "risk-on"))
        with self.assertRaises(journal.JournalError):
            journal.new_entry(doc, self.plan(), "suggested")           # duplicate
        with self.assertRaises(journal.JournalError):
            journal.new_entry(doc, self.plan(symbol="X", stop=15), "suggested")
        self.assertEqual(journal.new_entry(doc, self.plan(symbol="X"), "taken")["id"], 2)

    def bars(self, rows):
        return [{"date": d, "open": o, "high": h, "low": l, "close": c} for d, o, h, l, c in rows]

    def test_resolve_target_stop_time_and_running(self):
        e = journal.new_entry({"entries": []}, self.plan(), "suggested")
        b = self.bars([("2026-11-02", 14, 14.3, 13.9, 14.2), ("2026-11-03", 14.3, 14.6, 14.0, 14.5),
                       ("2026-11-04", 14.5, 18.6, 14.4, 18.0)])
        r = journal.resolve(e, b)
        self.assertEqual(r["outcome"], "target")
        self.assertEqual(r["entry"], 14.3)                # next day's open
        self.assertAlmostEqual(r["r"], (17.5 - 14.3) / (14.3 - 13.1), places=2)
        b2 = self.bars([("2026-11-03", 14.3, 14.6, 14.0, 14.5), ("2026-11-04", 13.0, 13.2, 12.5, 12.8)])
        self.assertEqual(journal.resolve(e, b2)["outcome"], "stop")
        self.assertEqual(journal.resolve(e, b2)["exit"], 13.0)       # gap fill
        b3 = self.bars([("2026-11-0%d" % d, 14.3, 14.6, 14.0, 14.4) for d in range(3, 6)])
        self.assertIsNone(journal.resolve(e, b3))                     # 3 of 5 days: still running
        b4 = self.bars([("2026-11-%02d" % d, 14.3, 14.6, 14.0, 14.4) for d in range(3, 10)])
        self.assertEqual(journal.resolve(e, b4)["outcome"], "time")

    def test_your_fills_win(self):
        e = journal.new_entry({"entries": []}, self.plan(), "taken")
        e.update(fill=14.0, exit=15.8, shares=40)
        r = journal.resolve(e, self.bars([("2026-11-03", 1, 1, 1, 1)]))
        self.assertEqual(r["source"], "your fills")
        self.assertAlmostEqual(r["r"], 2.0, places=2)

    def test_stats(self):
        entries = []
        doc = {"entries": entries}
        for k, (r, st, reg) in enumerate([(3, "taken", "risk-on"), (-1, "taken", "risk-on"),
                                          (-1, "suggested", "risk-off"), (None, "suggested", None)]):
            e = journal.new_entry(doc, self.plan(symbol="S%d" % k), st, reg)
            if r is not None:
                e["result"] = {"r": r, "entry": 14.0, "exit": 14.0 + r, "outcome": "x"}
                e["shares"] = 10
        s = journal.stats(entries)
        self.assertEqual((s["resolved"], s["open"]), (3, 1))
        self.assertEqual(s["all"]["avg_r"], 0.33)
        self.assertEqual(s["taken"]["n"], 2)
        self.assertEqual(s["by_regime"]["risk-on"]["avg_r"], 1.0)
        self.assertEqual(s["taken_pnl_usd"], 20.0)

    def test_cli_roundtrip(self):
        tmp = tempfile.mkdtemp()
        try:
            j = os.path.join(tmp, "j.json")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(journal.main(["--journal", j, "add", "--symbol", "ABC", "--entry", "10", "--stop", "9",
                                               "--target", "13", "--date", "2026-11-02",
                                               "--regime", os.path.join(tmp, "none")]), 0)
                self.assertEqual(journal.main(["--journal", j, "take", "1", "--shares", "20", "--fill", "10.1"]), 0)
                self.assertEqual(journal.main(["--journal", j, "close", "1", "--exit", "12.0", "--date", "2026-11-20"]), 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                journal.main(["--journal", j, "stats"])
            self.assertIn("ones you took", buf.getvalue())
            self.assertIn("Only 1 scored", buf.getvalue())
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
