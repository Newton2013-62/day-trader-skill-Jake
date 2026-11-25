import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
from test_edge import chart

sysm = load("trader-swing", "systems")


def flat_dates(n):
    import datetime
    from test_edge import trading_days
    return [d.isoformat() for d in trading_days(datetime.date(2026, 11, 24), n)]


class Pieces(unittest.TestCase):
    def test_month_ends(self):
        d = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-27", "2026-03-02"]
        self.assertEqual(sysm.month_ends(d), [1, 3])

    def test_ret_n_and_skip(self):
        s = [1.0, 2.0, 4.0, 8.0]
        self.assertEqual(sysm.ret_n(s, 3, 3), 7.0)
        self.assertEqual(sysm.ret_n(s, 3, 3, 1), 3.0)
        self.assertIsNone(sysm.ret_n(s, 1, 3))
        self.assertIsNone(sysm.ret_n([None, 1.0, 2.0], 2, 2))

    def test_buy_and_hold_weights_match_the_asset(self):
        dates = flat_dates(300)
        px = [100 * 1.001 ** i for i in range(300)]
        curve, trades, inv = sysm.run_weights(dates, {"SPY": px, "BIL": [1.0] * 300},
                                              lambda i, k: {"SPY": 1.0}, 0, cost=0)
        mes = sysm.month_ends(dates)
        # invested from the day after the first month end
        self.assertAlmostEqual(curve[-1][1], px[-1] / px[mes[0] + 1], places=6)
        self.assertEqual(trades, 1)

    def test_cash_earns_nothing_and_costs_count(self):
        dates = flat_dates(100)
        flip = {"n": 0}

        def choose(i, k):
            flip["n"] += 1
            return {"SPY": 1.0} if flip["n"] % 2 else {"BIL": 1.0}
        curve, trades, _ = sysm.run_weights(dates, {"SPY": [100.0] * 100, "BIL": [1.0] * 100}, choose, 0)
        self.assertGreater(trades, 2)
        self.assertLess(curve[-1][1], 1.0)          # flat prices, so only costs

    def test_metrics(self):
        curve = [("2025-01-02", 1.0), ("2025-06-02", 1.2), ("2025-12-31", 0.9), ("2026-06-01", 1.1)]
        m = sysm.metrics(curve)
        self.assertEqual(m["end"], 3300.0)
        self.assertEqual(m["max_drawdown_pct"], 25.0)
        self.assertEqual(m["yearly"]["2025"], -10.0)

    def test_rsi2_rules(self):
        bars = sysm.parse_adj(chart(n=600, seed=21, vol=0.02, volume=5e6))
        tr = sysm.rsi2_trades("X", bars, {"min_price": 5, "min_dollar_volume": 20e6, "slippage": 0, "stops": False})
        self.assertGreater(len(tr), 3)
        idx = {b["date"]: i for i, b in enumerate(bars)}
        c = [b["close"] for b in bars]
        r2 = sysm.rsi_n(c, 2)
        for t in tr:
            i, j = idx[t["entry_date"]], idx[t["exit_date"]]
            self.assertLess(r2[i], 10)
            self.assertGreater(c[i], sum(c[i - 199:i + 1]) / 200)
            self.assertGreater(c[j], sum(c[j - 4:j + 1]) / 5)
        st = sysm.rsi2_trades("X", bars, {"min_price": 5, "min_dollar_volume": 20e6, "slippage": 0, "stops": True})
        for t in st:
            self.assertLessEqual(idx[t["exit_date"]] - idx[t["entry_date"]], 10)

    def test_rsi2_account_slots(self):
        dates = flat_dates(30)
        tr = [{"symbol": "S%d" % k, "entry_date": dates[2], "exit_date": dates[5], "entry": 10.0, "exit": 11.0,
               "rsi2": float(k), "ret": 0.1} for k in range(6)]
        curve, taken = sysm.rsi2_account(tr, dates, {})
        self.assertEqual(taken, 4)
        self.assertAlmostEqual(curve[-1][1], 3000 + 4 * 75 * 1.0, places=2)


class DailyOnly(unittest.TestCase):
    def test_monthly_bars_are_refused(self):
        doc = chart(n=80)
        res = doc["chart"]["result"][0]
        res["timestamp"] = [res["timestamp"][0] + k * 30 * 86400 for k in range(80)]
        with self.assertRaises(sysm.edge.EdgeError):
            sysm.parse_adj(doc)

    def test_daily_bars_pass(self):
        self.assertGreater(len(sysm.parse_adj(chart(n=80))), 70)


class EndToEnd(unittest.TestCase):
    def test_offline_run(self):
        tmp = tempfile.mkdtemp()
        try:
            cache = os.path.join(tmp, "c")
            os.makedirs(cache)
            syms = ["SPY", "BIL", "VEU", "AGG"] + sysm.SECTORS
            for k, s in enumerate(syms):
                vol = 0.0005 if s == "BIL" else 0.012
                with gzip.open(os.path.join(cache, "%s_all.json.gz" % s), "wt") as fh:
                    json.dump(chart(n=2900, seed=100 + k, vol=vol, drift=0.0003), fh)
            for k, s in enumerate(["AAA", "BBB"]):
                with gzip.open(os.path.join(cache, "%s_10y.json.gz" % s), "wt") as fh:
                    json.dump(chart(n=2600, seed=200 + k, vol=0.02, volume=5e6), fh)
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                rc = sysm.main(["--cache", cache, "--cache-hours", "1e9", "--pause", "0", "--rsi2-symbols", "AAA,BBB",
                                "--out", os.path.join(tmp, "s.json")])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            for s in ("LAST 5 YEARS", "LAST 10 YEARS", "Just holding SPY", "Faber", "Antonacci", "RSI(2)",
                      "Beat SPY's return", "not financial advice"):
                self.assertIn(s, out)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
