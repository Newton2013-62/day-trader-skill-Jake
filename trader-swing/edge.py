#!/usr/bin/env python3
"""Market regime and relative strength for swing trades. Standard library only.

  regime   is the market in an uptrend? SPY against its 50- and 200-day
           averages, with QQQ and IWM (small caps) beside it. Three states:
             risk-on   SPY above its 200-day and its 50-day, and the 50-day rising
             caution   SPY above its 200-day, but under its 50-day or the 50-day falling
             risk-off  SPY under its 200-day
           Breakouts fail more often when the whole market is falling. Writes
           trader/regime.json for the hunt, the journal and the backtest.
  rs       relative strength: how much each stock beat (or lagged) SPY over
           the last 1, 3 and 6 months, one weighted score, and its rank in
           the list. Reads trader/watchlist.json by default and, with
           --write, adds "rs" and "rs_rank" to every hit.

    python3 edge.py regime
    python3 edge.py rs                       # the watchlist's hits
    python3 edge.py rs --symbols NVDA,PLTR,SOFI
    python3 edge.py rs --write               # store the scores in the watchlist

Data: Yahoo's chart endpoint, daily bars (unofficial, can lag).
Education and tooling, not financial advice.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
BENCH = "SPY"
RS_WEIGHTS = ((21, 0.2), (63, 0.5), (126, 0.3))   # (trading days, weight): 1, 3 and 6 months
PAUSE = 0.3
SLEEP = time.sleep  # tests swap this out


class EdgeError(Exception):
    pass


# ---------------------------------------------------------------- data

def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym, rng="1y"):
    """Yahoo v8 daily chart JSON. query2 first, query1 on failure, one retry after a 429."""
    last = None
    for attempt in (0, 1):
        saw_429 = False
        for host in HOSTS:
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
                host, urllib.parse.quote(sym.upper().replace(".", "-")), rng)
            try:
                return json.loads(_get(url))
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 404:
                    raise EdgeError("Yahoo has no chart for %s" % sym.upper())
                if e.code == 429:
                    saw_429 = True
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = str(getattr(e, "reason", e))
        if attempt == 0 and saw_429:
            SLEEP(5)
            continue
        break
    raise EdgeError("could not get daily bars for %s from Yahoo (%s)" % (sym.upper(), last))


def parse_bars(doc, min_bars=1):
    """Yahoo chart JSON -> [{"date","open","high","low","close","volume"}], oldest first.
    Bars with a missing or zero price are dropped; a missing volume counts as 0."""
    try:
        res = doc["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        gmt = int((res.get("meta") or {}).get("gmtoffset", -18000))
    except (KeyError, IndexError, TypeError):
        raise EdgeError("the chart data has no daily bars")
    n = len(ts)

    def col(k):
        v = q.get(k) or []
        return list(v) + [None] * (n - len(v))
    o_, h_, l_, c_, v_ = (col(k) for k in ("open", "high", "low", "close", "volume"))
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = o_[i], h_[i], l_[i], c_[i]
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t + gmt)).date().isoformat()
        b = {"date": d, "open": float(o), "high": float(h), "low": float(l), "close": float(c),
             "volume": float(v_[i] or 0)}
        if bars and bars[-1]["date"] == d:
            bars[-1] = b     # a late duplicate (the live tick) wins
        else:
            bars.append(b)
    if len(bars) < min_bars:
        raise EdgeError("only %d daily bars; need at least %d" % (len(bars), min_bars))
    return bars


def cached_bars(sym, rng, cache_dir=None, max_age_hours=12, min_bars=1):
    """Daily bars, from a gzip cache in cache_dir when it is fresh enough, else from Yahoo."""
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, "%s_%s.json.gz" % (re.sub(r"[^A-Z0-9.-]", "_", sym.upper()), rng))
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_hours * 3600:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return parse_bars(json.load(fh), min_bars)
    doc = fetch_chart(sym, rng)
    bars = parse_bars(doc, min_bars)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(doc, fh)
    return bars


def read_json(path, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise EdgeError("no file at %s" % path)
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- regime

def sma(values, n, end=None):
    """Simple average of the n values ending at index end (inclusive, default the last)."""
    end = len(values) - 1 if end is None else end
    if end + 1 < n:
        return None
    return sum(values[end - n + 1:end + 1]) / float(n)


def regime_at(closes, i, slope_days=10):
    """The regime on day i from a close series (oldest first). Pure.
    -> (state, numbers) where state is risk-on, caution, risk-off or None (too little data)."""
    s200 = sma(closes, 200, i)
    s50 = sma(closes, 50, i)
    s50_then = sma(closes, 50, i - slope_days) if i - slope_days >= 0 else None
    if s200 is None or s50 is None or s50_then is None:
        return None, {}
    c = closes[i]
    rising = s50 > s50_then
    if c < s200:
        state = "risk-off"
    elif c > s50 and rising:
        state = "risk-on"
    else:
        state = "caution"
    return state, {"close": round(c, 2), "sma50": round(s50, 2), "sma200": round(s200, 2),
                   "sma50_rising": rising, "pct_vs_200": round((c / s200 - 1) * 100, 1),
                   "pct_vs_50": round((c / s50 - 1) * 100, 1)}


def regime_series(bars, slope_days=10):
    """{date: state} for every day with enough history. Pure."""
    closes = [b["close"] for b in bars]
    out = {}
    for i, b in enumerate(bars):
        st, _ = regime_at(closes, i, slope_days)
        if st:
            out[b["date"]] = st
    return out


ADVICE = {
    "risk-on": "The market is in an uptrend. Breakout and flag setups are working in their best conditions.",
    "caution": "The market is above its long-term trend but has lost its short-term one. Take fewer, "
               "stronger setups, or smaller ones.",
    "risk-off": "The market is under its 200-day average. Most breakouts fail in this tape. "
                "The default is no new swing entries.",
}


def cmd_regime(a):
    rows = {}
    for sym in ("SPY", "QQQ", "IWM"):
        try:
            bars = cached_bars(sym, "2y", a.cache, min_bars=220)
        except EdgeError as e:
            if sym == "SPY":
                raise
            rows[sym] = {"error": str(e)}
            continue
        closes = [b["close"] for b in bars]
        st, nums = regime_at(closes, len(closes) - 1)
        rows[sym] = dict(nums, state=st, as_of=bars[-1]["date"])
        if a.pause:
            SLEEP(a.pause)
    spy = rows["SPY"]
    doc = {"created_by": "trader-swing/edge", "as_of": spy["as_of"], "state": spy["state"],
           "advice": ADVICE[spy["state"]], "markets": rows}
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("MARKET REGIME as of the %s close: %s" % (spy["as_of"], spy["state"].upper()))
        names = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000 (small caps)"}
        for sym, r in rows.items():
            if "error" in r:
                print("  %-4s %s" % (sym, r["error"]))
                continue
            print("  %-4s %-26s %9.2f   %+5.1f%% vs 200-day, %+5.1f%% vs 50-day, 50-day %s  -> %s"
                  % (sym, names[sym], r["close"], r["pct_vs_200"], r["pct_vs_50"],
                     "rising" if r["sma50_rising"] else "falling", r["state"]))
        print("  " + ADVICE[spy["state"]])
        print("The regime is a filter, not a forecast: it says what kind of market this is, not where it goes next.")
    if not a.no_write:
        write_json(a.out, doc)
        if not a.json:
            print("Wrote %s." % a.out)
    return 0


# ---------------------------------------------------------------- relative strength

def ret(closes, n, end=None):
    end = len(closes) - 1 if end is None else end
    if end - n < 0:
        return None
    return closes[end] / closes[end - n] - 1


def rs_score(closes, bench, end=None, bench_end=None):
    """Weighted excess return over the benchmark, in percent. Pure.
    closes/bench: close lists aligned on their last bar (or explicit end indexes).
    Periods the stock is too young for are left out and the weights renormalized."""
    tot, wsum, parts = 0.0, 0.0, {}
    for n, w in RS_WEIGHTS:
        r = ret(closes, n, end)
        b = ret(bench, n, bench_end)
        if r is None or b is None:
            continue
        ex = (r - b) * 100
        parts["%dd" % n] = round(ex, 1)
        tot += w * ex
        wsum += w
    if not wsum:
        return None, parts
    return round(tot / wsum, 1), parts


def rank_pct(scores):
    """{sym: score} -> {sym: percentile 1-99}, higher is stronger. Pure."""
    items = sorted((s, k) for k, s in scores.items() if s is not None)
    n = len(items)
    out = {}
    for i, (_, k) in enumerate(items):
        out[k] = 99 if n == 1 else int(round(1 + 98.0 * i / (n - 1)))
    return out


def cmd_rs(a):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
        wl = None
    else:
        wl = read_json(a.watchlist)
        syms = [h["symbol"] for h in wl.get("hits", [])]
        if a.limit:
            syms = syms[:a.limit]
    if not syms:
        raise EdgeError("no symbols: pass --symbols or run the scanner first")
    bench = [b["close"] for b in cached_bars(BENCH, "1y", a.cache, min_bars=130)]
    scores, parts, failed = {}, {}, {}
    for k, sym in enumerate(syms):
        if k and a.pause:
            SLEEP(a.pause)
        try:
            closes = [b["close"] for b in cached_bars(sym, "1y", a.cache, min_bars=22)]
        except EdgeError as e:
            failed[sym] = str(e)
            continue
        scores[sym], parts[sym] = rs_score(closes, bench)
        if not a.json and len(syms) > 40 and (k + 1) % 50 == 0:
            print("  %d of %d" % (k + 1, len(syms)), file=sys.stderr)
    ranks = rank_pct(scores)
    rows = sorted(({"symbol": s, "rs": scores[s], "rs_rank": ranks.get(s), "vs_spy": parts[s]}
                   for s in scores if scores[s] is not None), key=lambda r: -r["rs"])
    if a.json:
        print(json.dumps({"rows": rows, "failed": failed}, indent=2))
    else:
        print("RELATIVE STRENGTH vs SPY (weighted: 1 month 20%, 3 months 50%, 6 months 30%), strongest first")
        print("  %-6s %7s %5s   %8s %8s %8s" % ("symbol", "score", "rank", "1 mo", "3 mo", "6 mo"))
        for r in rows[:a.show]:
            v = r["vs_spy"]
            print("  %-6s %+6.1f%% %5d   %s %s %s" % (
                r["symbol"], r["rs"], r["rs_rank"],
                *("%+7.1f%%" % v[k] if k in v else "      - " for k in ("21d", "63d", "126d"))))
        if len(rows) > a.show:
            print("  ... and %d more (--show N)." % (len(rows) - a.show))
        if failed:
            print("  No data for %d: %s" % (len(failed), ", ".join(sorted(failed)[:10])))
        print("A positive score means the stock beat the S&P 500 over those months. Leaders tend to keep "
              "leading for a while, but not always.")
    if a.write and wl is not None:
        for h in wl.get("hits", []):
            if h["symbol"] in scores:
                h["rs"] = scores[h["symbol"]]
                h["rs_rank"] = ranks.get(h["symbol"])
        wl["rs_note"] = "rs = weighted excess return vs SPY in percent; rs_rank 1-99 within this list"
        write_json(a.watchlist, wl)
        if not a.json:
            print("Wrote rs and rs_rank into %s." % a.watchlist)
    return 0 if rows else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Market regime and relative strength for swing trades.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("regime", help="is the market in an uptrend? (SPY vs its 50- and 200-day averages)")
    p.add_argument("--out", default="trader/regime.json")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rs", help="relative strength vs SPY, ranked")
    p.add_argument("--symbols")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", type=int, default=40)
    p.add_argument("--write", action="store_true", help="add rs and rs_rank to the watchlist's hits")
    p.add_argument("--json", action="store_true")
    for p in sub.choices.values():
        p.add_argument("--cache", default="trader/cache/daily", help="where downloaded bars are kept")
        p.add_argument("--pause", type=float, default=PAUSE)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"regime": cmd_regime, "rs": cmd_rs}[a.cmd](a)
    except EdgeError as e:
        print("edge.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
