#!/usr/bin/env python3
"""Backtest well-known, published trading systems against just holding SPY. Standard library only.

Instead of inventing rules, test the ones with a long public record, coded the
way their authors published them:

  faber      Meb Faber's trend filter ("A Quantitative Approach to Tactical
             Asset Allocation", 2007): at each month end, hold SPY if it closed
             above its 10-month average, otherwise T-bills (BIL).
  dual       Gary Antonacci's Global Equities Momentum ("Dual Momentum
             Investing", 2014): at each month end, if SPY's 12-month return
             beats T-bills, hold the stronger of SPY and international stocks
             (VEU); otherwise hold bonds (AGG).
  sectors    Sector momentum rotation: at each month end, hold the 3 S&P
             sector ETFs with the best 12-month return skipping the last month
             (the classic 12-1 momentum), equal weight.
  sectors200 The same, but in T-bills when SPY is under its 10-month average.
  rsi2       Larry Connors' RSI(2) pullback ("Short Term Trading Strategies
             That Work", 2008), on liquid US stocks: buy at the close when the
             stock is above its 200-day average and its 2-day RSI is under 10;
             sell at the close when it closes above its 5-day average.
             Lowest RSI first, up to 4 positions of a quarter of the account.
  rsi2stop   The same with a 10-day time stop and a stop 2.5 ATR under the entry.

The ETF systems use dividend-adjusted closes (total return) and trade one day
after the signal, with 0.1 % cost per unit of turnover. Each is run over the
last 5 and 10 years and compared with SPY bought and held over the same days.

    python3 systems.py                       # every system, 5 and 10 years
    python3 systems.py --only faber,dual
    python3 systems.py --rsi2-sample 400     # stocks sampled from trader/universe.txt

The RSI(2) test uses TODAY's stock list (survivorship bias: delisted stocks are
missing), so treat its numbers as better than reality. Published systems also
tend to work less well after they are published. Education and tooling, not
financial advice.
"""

import argparse
import datetime
import gzip
import importlib.util
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


edge = _load(os.path.join(HERE, "edge.py"), "trader_edge_sys")
bt = _load(os.path.join(HERE, "backtest.py"), "trader_backtest_sys")

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLRE", "XLC"]
COST = 0.001
ETF_SYSTEMS = ["faber", "dual", "sectors", "sectors200"]
ALL_SYSTEMS = ETF_SYSTEMS + ["rsi2", "rsi2stop"]
NAMES = {"faber": "Faber 10-month trend (SPY or T-bills)",
         "dual": "Antonacci dual momentum (SPY/VEU/AGG)",
         "sectors": "Sector momentum, top 3",
         "sectors200": "Sector momentum, top 3 + trend filter",
         "rsi2": "Connors RSI(2) pullbacks, stocks",
         "rsi2stop": "RSI(2) + 10-day and 2.5 ATR stops",
         "spy": "Just holding SPY"}


class SystemsError(Exception):
    pass


# ---------------------------------------------------------------- data

def parse_adj(doc):
    """Yahoo chart JSON -> [{"date","open","high","low","close","adj","volume"}]; adj = dividend-adjusted close."""
    bars = edge.parse_bars(doc)
    try:
        res = doc["chart"]["result"][0]
        adj = ((res["indicators"].get("adjclose") or [{}])[0] or {}).get("adjclose")
        ts = res.get("timestamp") or []
        gmt = int((res.get("meta") or {}).get("gmtoffset", -18000))
    except (KeyError, IndexError, TypeError):
        adj = None
    by_date = {}
    if adj:
        for t, a in zip(ts, adj):
            if a:
                d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t + gmt)).date().isoformat()
                by_date[d] = float(a)
    for b in bars:
        b["adj"] = by_date.get(b["date"], b["close"])
    return bars


def load(sym, rng, cache, hours, pause):
    path = os.path.join(cache, "%s_%s.json.gz" % (re.sub(r"[^A-Z0-9.-]", "_", sym.upper()), rng))
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < hours * 3600:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return parse_adj(json.load(fh))
    doc = edge.fetch_chart(sym, rng)
    os.makedirs(cache, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)
    if pause:
        edge.SLEEP(pause)
    return parse_adj(doc)


# ---------------------------------------------------------------- pure pieces

def month_ends(dates):
    """Indexes of the last trading day of each month."""
    return [i for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]]


def ret_n(series, i, n, skip=0):
    """Return from i-n to i-skip on an aligned list (None where data is missing)."""
    a, b = i - n, i - skip
    if a < 0 or series[a] is None or series[b] is None:
        return None
    return series[b] / series[a] - 1


def sma_months(series, mes, k, n):
    """Average of the last n month-end values up to month-end number k."""
    if k + 1 < n:
        return None
    vals = [series[mes[j]] for j in range(k - n + 1, k + 1)]
    return None if None in vals else sum(vals) / n


def run_weights(dates, adj, choose, start_i, cost=COST):
    """Monthly rebalancing on dividend-adjusted closes. Pure.
    adj: {sym: [price or None, aligned on dates]}; choose(i, k) -> {sym: weight} at month-end index i.
    Trades one day after the signal. -> (curve [(date, value)], trades, invested share of days)"""
    mes = month_ends(dates)
    k_of = {i: k for k, i in enumerate(mes)}
    w = {}
    value = 1.0
    curve = []
    trades = 0
    pending = None
    invested = 0
    for i in range(start_i, len(dates)):
        if i > start_i:
            r = 0.0
            for s, wt in w.items():
                p0, p1 = adj[s][i - 1], adj[s][i]
                if p0 and p1:
                    r += wt * (p1 / p0 - 1)
            value *= 1 + r
        if pending is not None:
            turn = sum(abs(pending.get(s, 0) - w.get(s, 0)) for s in set(pending) | set(w))
            if turn > 1e-9:
                trades += 1
                value *= 1 - cost * turn
            w = pending
            pending = None
        if any(s not in ("BIL",) for s in w):
            invested += 1
        curve.append((dates[i], value))
        if i in k_of:
            pending = choose(i, k_of[i])
    return curve, trades, invested / max(1, len(curve))


def metrics(curve, start_value=3000.0):
    if len(curve) < 2:
        return None
    v0 = curve[0][1]
    peak, dd = v0, 0.0
    years = {}
    for d, v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak)
        years.setdefault(d[:4], [v, v])[1] = v
    prev = v0
    yearly = {}
    for y in sorted(years):
        first, last = years[y]
        yearly[y] = round((last / prev - 1) * 100, 1)
        prev = last
    n_years = max(1e-9, (datetime.date.fromisoformat(curve[-1][0]) -
                         datetime.date.fromisoformat(curve[0][0])).days / 365.25)
    end = curve[-1][1] / v0
    return {"end": round(start_value * end, 2), "return_pct": round((end - 1) * 100, 1),
            "cagr_pct": round((end ** (1 / n_years) - 1) * 100, 1), "max_drawdown_pct": round(dd * 100, 1),
            "worst_year_pct": min(yearly.values()), "yearly": yearly}


def rsi_n(closes, n):
    return bt.rsi_series(closes, n)


def rsi2_trades(sym, bars, rules):
    """Connors RSI(2): every entry and exit in one stock. Pure.
    Buy at the close when close > SMA200 and RSI(2) < 10; sell at the close when close > SMA5.
    With rules["stops"]: also out after 10 days or at a stop 2.5 ATR under the entry."""
    c = [b["close"] for b in bars]
    r2 = rsi_n(c, 2)
    out = []
    i = 200
    while i < len(bars) - 1:
        b = bars[i]
        if c[i] < rules["min_price"]:
            i += 1
            continue
        dv = sum(x["close"] * x["volume"] for x in bars[i - 19:i + 1]) / 20.0
        if dv < rules["min_dollar_volume"] or r2[i] is None or r2[i] >= 10:
            i += 1
            continue
        if c[i] <= sum(c[i - 199:i + 1]) / 200.0:
            i += 1
            continue
        entry = c[i] * (1 + rules["slippage"])
        a = bt.atr_at(bars, i)
        stop = entry - 2.5 * a if (rules["stops"] and a) else None
        j = i + 1
        exit_px, why = None, None
        while j < len(bars):
            if stop is not None and bars[j]["open"] <= stop:
                exit_px, why = bars[j]["open"], "stop"
                break
            if stop is not None and bars[j]["low"] <= stop:
                exit_px, why = stop, "stop"
                break
            if c[j] > sum(c[j - 4:j + 1]) / 5.0:
                exit_px, why = c[j], "sma5"
                break
            if rules["stops"] and j - i >= 10:
                exit_px, why = c[j], "time"
                break
            j += 1
        if exit_px is None:
            break           # still open at the end of the data
        exit_px *= 1 - rules["slippage"]
        out.append({"symbol": sym, "entry_date": b["date"], "exit_date": bars[j]["date"], "entry": entry,
                    "exit": exit_px, "rsi2": r2[i], "why": why, "ret": exit_px / entry - 1})
        i = j + 1
    return out


def rsi2_account(trades, dates, closes_by_sym, slots=4, start=3000.0):
    """Up to `slots` positions, each a 1/slots share of the account value at entry,
    lowest RSI(2) first. Entries and exits at the close. Marked to market daily. Pure."""
    by_entry = {}
    for t in trades:
        by_entry.setdefault(t["entry_date"], []).append(t)
    cash = start
    pos = []
    curve = []
    taken = 0
    last = {}
    for d in dates:
        keep = []
        for p in pos:
            if p["t"]["exit_date"] <= d:
                cash += p["shares"] * p["t"]["exit"]
            else:
                keep.append(p)
        pos = keep
        value = cash + sum(p["shares"] * last.get(p["t"]["symbol"], p["t"]["entry"]) for p in pos)
        for t in sorted(by_entry.get(d, []), key=lambda t: t["rsi2"]):
            if len(pos) >= slots or any(p["t"]["symbol"] == t["symbol"] for p in pos):
                continue
            spend = min(value / slots, cash)
            shares = int(spend / t["entry"])
            if shares < 1:
                continue
            cash -= shares * t["entry"]
            pos.append({"t": t, "shares": shares})
            taken += 1
        for p in pos:
            px = closes_by_sym.get(p["t"]["symbol"], {}).get(d)
            if px is not None:
                last[p["t"]["symbol"]] = px
        curve.append((d, cash + sum(p["shares"] * last.get(p["t"]["symbol"], p["t"]["entry"]) for p in pos)))
    return curve, taken


# ---------------------------------------------------------------- run

def run(a):
    want = [s for s in (a.only.split(",") if a.only else ALL_SYSTEMS) if s]
    bad = [s for s in want if s not in ALL_SYSTEMS]
    if bad:
        raise SystemsError("unknown system %s; use %s" % (", ".join(bad), ", ".join(ALL_SYSTEMS)))
    log = (lambda m: None) if a.json else (lambda m: print(m, file=sys.stderr))
    etfs = ["SPY", "BIL", "VEU", "AGG"] + SECTORS
    data = {}
    for s in etfs:
        try:
            data[s] = load(s, "all", a.cache, a.cache_hours, a.pause)
        except edge.EdgeError as e:
            if s in ("SPY", "BIL"):
                raise
            log("  %s: %s" % (s, e))
    spy = data["SPY"]
    dates = [b["date"] for b in spy]
    adj = {s: [None] * len(dates) for s in data}
    pos = {d: i for i, d in enumerate(dates)}
    for s, bars in data.items():
        for b in bars:
            i = pos.get(b["date"])
            if i is not None:
                adj[s][i] = b["adj"]
    mes = month_ends(dates)

    def faber(i, k):
        m = sma_months(adj["SPY"], mes, k, 10)
        return {"SPY": 1.0} if m and adj["SPY"][i] > m else {"BIL": 1.0}

    def dual(i, k):
        spy12, bil12 = ret_n(adj["SPY"], i, 252), ret_n(adj["BIL"], i, 252)
        veu12 = ret_n(adj.get("VEU", adj["SPY"]), i, 252)
        if spy12 is None or bil12 is None:
            return {"BIL": 1.0}
        if spy12 > bil12:
            return {"VEU": 1.0} if veu12 is not None and veu12 > spy12 else {"SPY": 1.0}
        return {"AGG": 1.0} if "AGG" in adj else {"BIL": 1.0}

    def sectors(i, k, trend=False):
        if trend:
            m = sma_months(adj["SPY"], mes, k, 10)
            if not m or adj["SPY"][i] <= m:
                return {"BIL": 1.0}
        scored = [(ret_n(adj[s], i, 252, 21), s) for s in SECTORS if s in adj]
        scored = sorted([x for x in scored if x[0] is not None], reverse=True)[:3]
        if not scored:
            return {"BIL": 1.0}
        return {s: 1.0 / len(scored) for _, s in scored}

    choosers = {"faber": faber, "dual": dual, "sectors": sectors,
                "sectors200": lambda i, k: sectors(i, k, True)}
    windows = [y for y in (5, 10) if y <= a.max_years]
    last_day = datetime.date.fromisoformat(dates[-1])
    starts = {y: next(i for i, d in enumerate(dates)
                      if d >= (last_day - datetime.timedelta(days=int(365.25 * y))).isoformat()) for y in windows}
    res = {"created_by": "trader-swing/systems", "to": dates[-1], "windows": {}}
    stock_trades = {}
    if any(s.startswith("rsi2") for s in want):
        syms = pick(a)
        log("RSI(2): %d stocks..." % len(syms))
        closes_by_sym = {}
        for n, sym in enumerate(syms):
            try:
                bars = load(sym, "10y" if a.max_years > 5 else "5y", a.cache, a.cache_hours, a.pause)
            except edge.EdgeError:
                continue
            if len(bars) < 260:
                continue
            closes_by_sym[sym] = {b["date"]: b["close"] for b in bars}
            for key, stops in (("rsi2", False), ("rsi2stop", True)):
                if key in want:
                    stock_trades.setdefault(key, []).extend(rsi2_trades(sym, bars, {
                        "min_price": 5.0, "min_dollar_volume": a.rsi2_min_dv, "slippage": 0.001, "stops": stops}))
            if (n + 1) % 50 == 0:
                log("  %d of %d" % (n + 1, len(syms)))
        res["rsi2_stocks"] = len(closes_by_sym)
    for y in windows:
        s0 = starts[y]
        row = {}
        spy_curve = [(dates[i], adj["SPY"][i]) for i in range(s0, len(dates))]
        row["spy"] = metrics(spy_curve)
        for s in want:
            if s in choosers:
                curve, trades, inv = run_weights(dates, adj, choosers[s], s0)
                m = metrics(curve)
                m.update(trades=trades, invested_pct=round(inv * 100))
                row[s] = m
            else:
                tr = [t for t in stock_trades.get(s, []) if t["entry_date"] >= dates[s0]]
                curve, taken = rsi2_account(tr, dates[s0:], closes_by_sym)
                m = metrics(curve)
                if m:
                    wins = sum(1 for t in tr if t["ret"] > 0)
                    m.update(trades=taken, signals=len(tr),
                             win_pct=round(100.0 * wins / len(tr), 1) if tr else None,
                             avg_trade_pct=round(100 * sum(t["ret"] for t in tr) / len(tr), 2) if tr else None)
                row[s] = m
        res["windows"]["%dy" % y] = {"from": dates[s0], "rows": row}
    return res


def pick(a):
    if a.rsi2_symbols:
        return [s for s in re.split(r"[,\s]+", a.rsi2_symbols.upper()) if s]
    path = a.universe
    if not os.path.exists(path):
        raise SystemsError("no %s: run scan.py universe first, or pass --rsi2-symbols" % path)
    syms = bt.scan.symbols_from_file(path)
    return sorted(random.Random(a.seed).sample(syms, min(a.rsi2_sample, len(syms))))


def render(res):
    L = ["PUBLISHED SYSTEMS vs JUST HOLDING SPY, $3,000 start, through %s (total return, 0.1%% costs)" % res["to"]]
    for w, blk in res["windows"].items():
        L.append("")
        L.append("LAST %s YEARS (from %s)" % (w[:-1], blk["from"]))
        L.append("  %-42s %9s %7s %10s %10s %7s" % ("system", "end", "CAGR", "worst drop", "worst year", "trades"))
        rows = blk["rows"]
        for key in ["spy"] + [k for k in ALL_SYSTEMS if k in rows]:
            m = rows.get(key)
            if not m:
                continue
            L.append("  %-42s %9s %6.1f%% %9.1f%% %9.1f%% %7s" % (
                NAMES[key], "${:,.0f}".format(m["end"]), m["cagr_pct"], m["max_drawdown_pct"], m["worst_year_pct"],
                m.get("trades", "-")))
        spy = rows["spy"]
        better = [NAMES[k] for k in ALL_SYSTEMS if k in rows and rows[k]
                  and rows[k]["cagr_pct"] > spy["cagr_pct"]]
        safer = [NAMES[k] for k in ALL_SYSTEMS if k in rows and rows[k]
                 and rows[k]["max_drawdown_pct"] < spy["max_drawdown_pct"] - 5
                 and rows[k]["cagr_pct"] > spy["cagr_pct"] - 2]
        L.append("  Beat SPY's return: %s." % (", ".join(better) if better else "none"))
        L.append("  Close to SPY's return with a much smaller worst drop: %s." % (", ".join(safer) if safer else "none"))
    if "rsi2_stocks" in res:
        L.append("")
        L.append("RSI(2) ran on %d liquid US stocks from today's list (delisted stocks are missing: flattering)."
                 % res["rsi2_stocks"])
        for w, blk in res["windows"].items():
            for k in ("rsi2", "rsi2stop"):
                m = blk["rows"].get(k)
                if m and m.get("signals"):
                    L.append("  %s %s: %d signals, %.1f%% won, %+.2f%% average trade before account limits."
                             % (w, NAMES[k], m["signals"], m["win_pct"], m["avg_trade_pct"]))
    L.append("")
    L.append("Published systems often do worse after they are published, and past returns are not a forecast. "
             "Education and tooling, not financial advice.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest published trading systems against holding SPY.")
    ap.add_argument("--only", help="comma list: " + ",".join(ALL_SYSTEMS))
    ap.add_argument("--max-years", type=int, default=10, choices=[5, 10])
    ap.add_argument("--universe", default="trader/universe.txt")
    ap.add_argument("--rsi2-sample", type=int, default=400)
    ap.add_argument("--rsi2-symbols")
    ap.add_argument("--rsi2-min-dv", type=float, default=20e6, help="20-day dollar volume floor (default $20M)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache", default="trader/cache/daily")
    ap.add_argument("--cache-hours", type=float, default=20)
    ap.add_argument("--pause", type=float, default=0.25)
    ap.add_argument("--out", default="trader/systems.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run(a)
    except (SystemsError, edge.EdgeError) as e:
        print("systems.py: %s" % e, file=sys.stderr)
        return 1
    if a.out:
        edge.write_json(a.out, res)
    print(json.dumps(res, indent=2) if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
