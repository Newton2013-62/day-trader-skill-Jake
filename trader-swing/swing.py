#!/usr/bin/env python3
"""Swing trades for the Day Trader pack: hold days to weeks, not minutes. Standard library only.

  plan     builds a swing plan from daily bars: entry, a stop under the noise
           (2 x ATR by default, or the 10-day low, a percent, or your price),
           a target (2R by default), a max holding period (15 trading days by
           default) and an earnings check for that window. Writes
           trader/plan.json with "style": "swing", which trader-budget turns
           into a good-till-canceled bracket (the stop stays at the broker
           overnight) and trader-risk checks with the swing rules.
  replay   the multi-day Time Machine: rebuilds the next N trading days 10,000
           times from the stock's own daily bars (gaps included), and re-runs
           the same stop and target distances from every past day. Prints
           target first / stop first / still open at the time limit, the
           average R, and one honest verdict.
  earnings the estimated next earnings date (SEC EDGAR 8-K item 2.02 history).

    python3 swing.py plan NVDA --write                        # entry = last close
    python3 swing.py plan NVDA --entry 181.50 --stop low10 --target 2R --hold 10 --write
    python3 swing.py replay                                   # reads trader/plan.json
    python3 swing.py earnings NVDA

Data: Yahoo's chart endpoint, 2 years of daily bars (unofficial, can lag).
Education and tooling, not financial advice.
"""

import argparse
import datetime
import json
import os
import random
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK%010d.json"
DEFAULTS = {"swing_hold_days": 15, "swing_stop": "atr", "swing_atr_mult": 2.0, "swing_target_r": 2.0,
            "min_reward_risk": 1.0}
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


class SwingError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cent(x):
    return int(round(float(x) * 100)) / 100.0


def floor_cent(x):
    return int(float(x) * 100 + 1e-9) / 100.0


def p2(x):
    return "%.2f" % float(x)


def money(x):
    s = "{:,.2f}".format(float(x))
    return "$" + (s[:-3] if s.endswith(".00") else s)


def read_json(path, required=True, what="file"):
    if not path or not os.path.exists(path):
        if required:
            raise SwingError("no %s at %s" % (what, path))
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


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def add_trading_days(d, n):
    """The date n NYSE trading days after d (weekends and the 2026-2027 holidays skipped)."""
    out = d
    left = n
    while left > 0:
        out += datetime.timedelta(days=1)
        if trading_day(out):
            left -= 1
    return out


def ny_today():
    utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    # New York is UTC-4 in daylight time, UTC-5 otherwise; the date is all we need here.
    y = utc.year
    mar = datetime.date(y, 3, 1)
    dst_start = datetime.datetime.combine(mar + datetime.timedelta(days=(6 - mar.weekday()) % 7 + 7),
                                          datetime.time(7))
    nov = datetime.date(y, 11, 1)
    dst_end = datetime.datetime.combine(nov + datetime.timedelta(days=(6 - nov.weekday()) % 7), datetime.time(6))
    off = -4 if dst_start <= utc < dst_end else -5
    return (utc + datetime.timedelta(hours=off)).date()


# ---------------------------------------------------------------- data

def _get(url, ua=UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_daily(sym, rng="2y"):
    last = None
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
            host, urllib.parse.quote(sym.upper().replace(".", "-")), rng)
        try:
            return json.loads(_get(url))
        except urllib.error.HTTPError as e:
            last = e.code
            if e.code == 404:
                raise SwingError("Yahoo has no chart for %s: check the ticker" % sym.upper())
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
    raise SwingError("could not get daily bars for %s from Yahoo (%s). Try again later." % (sym.upper(), last))


def parse_daily(doc):
    """Yahoo chart JSON -> [{"date", "open", "high", "low", "close"}], oldest first, gaps dropped."""
    try:
        res = doc["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        gmt = int(res.get("meta", {}).get("gmtoffset", -18000))
    except (KeyError, IndexError, TypeError):
        raise SwingError("the chart data has no daily bars")
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        d = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t + gmt)).date()
        if bars and bars[-1]["date"] == d.isoformat():
            bars[-1] = {"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c}
        else:
            bars.append({"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c})
    if len(bars) < 60:
        raise SwingError("only %d daily bars; need at least 60" % len(bars))
    return bars


def load_bars(sym, offline=None):
    doc = read_json(offline, what="offline chart") if offline else fetch_daily(sym)
    return parse_daily(doc)


def atr(bars, n=14):
    """Average true range over the last n bars (simple average)."""
    if len(bars) < n + 1:
        raise SwingError("need %d bars for a %d-day ATR" % (n + 1, n))
    trs = []
    for i in range(len(bars) - n, len(bars)):
        b, prev = bars[i], bars[i - 1]["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev)))
    return sum(trs) / n


# ---------------------------------------------------------------- earnings (SEC EDGAR)

def sec_user_agent(config_path):
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        cfg = read_json(config_path, required=False) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "you@example.com" in ua:
        return None
    return ua


def earnings_dates(sym, ua, fetch=None):
    """Past earnings 8-K (item 2.02) filing dates for sym, newest first."""
    fetch = fetch or (lambda url: json.loads(_get(url, ua=ua, timeout=30)))
    tickers = fetch(SEC_TICKERS)
    cik = None
    for row in tickers.values():
        if str(row.get("ticker", "")).upper() == sym.upper():
            cik = int(row["cik_str"])
            break
    if cik is None:
        raise SwingError("SEC has no company for %s (ETFs and many ADRs have no earnings filings)" % sym.upper())
    rec = fetch(SEC_SUBMISSIONS % cik).get("filings", {}).get("recent", {})
    out = []
    items = rec.get("items", [])
    for i, form in enumerate(rec.get("form", [])):
        if form == "8-K" and i < len(items) and "2.02" in (items[i] or ""):
            out.append(rec["filingDate"][i])
    return out


def estimate_next(dates):
    """(estimated next date, typical spacing in days) from past earnings dates, or (None, None)."""
    ds = sorted({datetime.date.fromisoformat(d) for d in dates})
    if not ds:
        return None, None
    gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if 60 <= (b - a).days <= 120]
    gap = int(statistics.median(gaps)) if gaps else 91
    nxt = ds[-1] + datetime.timedelta(days=gap)
    return nxt, gap


def earnings_check(sym, start, end, override=None, ua=None, fetch=None):
    """A dict saying whether earnings likely fall inside [start, end]."""
    if override:
        d = datetime.date.fromisoformat(override)
        inside = start <= d <= end
        return {"date": d.isoformat(), "source": "you", "inside": inside, "estimated": False}
    if not ua:
        return {"date": None, "source": None, "inside": None, "estimated": True,
                "note": "earnings date unknown: set \"sec_user_agent\" in trader/config.json, or pass "
                        "--earnings YYYY-MM-DD from the company's investor-relations page"}
    try:
        nxt, gap = estimate_next(earnings_dates(sym, ua, fetch))
    except SwingError as e:
        return {"date": None, "source": "SEC", "inside": None, "estimated": True, "note": str(e)}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return {"date": None, "source": "SEC", "inside": None, "estimated": True,
                "note": "could not read SEC filings (%s); check the date yourself" % e}
    if nxt is None:
        return {"date": None, "source": "SEC", "inside": None, "estimated": True,
                "note": "no earnings 8-K filings found for %s" % sym.upper()}
    # an estimate can be off by a week or two either way
    slack = datetime.timedelta(days=10)
    inside = (start - slack) <= nxt <= (end + slack)
    while nxt < start - slack:
        nxt += datetime.timedelta(days=gap)
        inside = (start - slack) <= nxt <= (end + slack)
    return {"date": nxt.isoformat(), "source": "SEC (estimated from past filings, every ~%d days)" % gap,
            "inside": inside, "estimated": True}


# ---------------------------------------------------------------- the plan

def parse_r(text, default):
    t = str(text or "").strip().upper()
    if not t:
        return default, None
    if t.endswith("R"):
        r = _f(t[:-1])
        if not r or r <= 0:
            raise SwingError("--target %r: use 2R, 1.5R or a price" % text)
        return r, None
    price = _f(t)
    if not price or price <= 0:
        raise SwingError("--target %r: use 2R, 1.5R or a price" % text)
    return None, price


def make_plan(sym, bars, cfg, entry=None, stop_rule=None, atr_mult=None, target=None, hold=None,
              today=None, earnings=None):
    c = dict(DEFAULTS)
    c.update(cfg or {})
    sym = sym.strip().upper()
    last = bars[-1]
    entry = cent(entry) if entry else cent(last["close"])
    a = atr(bars)
    rule = str(stop_rule or c["swing_stop"]).strip().lower()
    mult = float(atr_mult or c["swing_atr_mult"])
    if rule == "atr":
        stop = floor_cent(entry - mult * a)
        label = "%s x the 14-day ATR (%s) under the entry" % (("%g" % mult), p2(a))
    elif rule.startswith("low"):
        n = int(rule[3:] or 10)
        stop = floor_cent(min(b["low"] for b in bars[-n:]) - 0.01)
        label = "a cent under the %d-day low" % n
    elif rule.endswith("%"):
        pct = _f(rule[:-1])
        if not pct or not 0 < pct < 50:
            raise SwingError("--stop %r: a percent like 6%%" % stop_rule)
        stop = floor_cent(entry * (1 - pct / 100.0))
        label = "%s%% under the entry" % ("%g" % pct)
    else:
        stop = _f(rule)
        if not stop:
            raise SwingError("--stop %r: use atr, low10, a percent like 6%% or a price" % stop_rule)
        stop = cent(stop)
        label = "your price"
    if stop <= 0 or stop >= entry:
        raise SwingError("the stop %s is not below the entry %s" % (p2(stop), p2(entry)))
    risk = cent(entry - stop)
    if risk < max(0.02, 0.25 * a) and rule != "atr":
        raise SwingError("the stop %s is only %s under the entry, less than a quarter of a normal day's "
                         "range (ATR %s): ordinary noise would hit it" % (p2(stop), p2(risk), p2(a)))
    r, tprice = parse_r(target, float(c["swing_target_r"]))
    if tprice:
        tgt = cent(tprice)
        tlabel = "your price"
    else:
        tgt = cent(entry + r * risk)
        tlabel = "%gR (%g times the risk)" % (r, r)
    if tgt <= entry:
        raise SwingError("the target %s is not above the entry %s" % (p2(tgt), p2(entry)))
    rr = round((tgt - entry) / risk, 2)
    hold = int(c["swing_hold_days"] if hold is None else hold)
    if not 1 <= hold <= 60:
        raise SwingError("--hold wants 1 to 60 trading days")
    day = today or ny_today()
    exit_by = add_trading_days(day, hold)
    warnings = []
    age = (day - datetime.date.fromisoformat(last["date"])).days
    if age > 4:
        warnings.append("the last daily bar is from %s, %d days before %s: the data is stale, check the price "
                        "before you trade" % (last["date"], age, day.isoformat()))
    if rr < float(c.get("min_reward_risk") or 0) - 1e-9:
        warnings.append("reward:risk %.2f is under your minimum %s: trader-risk will block it"
                        % (rr, c["min_reward_risk"]))
    if earnings:
        if earnings.get("inside"):
            warnings.append("EARNINGS likely inside the hold (%s, %s). A report can gap the stock past your "
                            "stop overnight; the loss can be much more than planned. Skip it, exit before, or "
                            "size smaller." % (earnings["date"], earnings["source"]))
        elif earnings.get("note"):
            warnings.append(earnings["note"])
    gap_note = ("A stop is an order, not a promise: overnight the stock can open below %s and the stop fills "
                "at the open, worse than planned." % p2(stop))
    return {
        "created_by": "trader-swing", "style": "swing", "symbol": sym, "side": "buy",
        "date": day.isoformat(), "setup": "swing", "entry": entry, "entry_time": "",
        "last_close": cent(last["close"]), "last_bar": last["date"], "atr14": round(a, 2),
        "stop": stop, "stop_rule": rule, "stop_label": label,
        "target": tgt, "target_label": tlabel, "risk_per_share": risk, "reward_risk": rr,
        "hold_days": hold, "exit_by": exit_by.isoformat(), "flat_by": None,
        "earnings": earnings, "warnings": warnings, "note": gap_note,
    }


def render_plan(p):
    lines = ["SWING PLAN %s (long), made %s from daily bars to %s" % (p["symbol"], p["date"], p["last_bar"]),
             "  Entry       %s%s" % (p2(p["entry"]), "  (last close)" if p["entry"] == p["last_close"] else ""),
             "  Stop        %s  %s" % (p2(p["stop"]), p["stop_label"]),
             "  Target      %s  %s" % (p2(p["target"]), p["target_label"]),
             "  Risk        %s a share, reward:risk %.2f" % (p2(p["risk_per_share"]), p["reward_risk"]),
             "  Hold        up to %d trading days: out by the close on %s if neither hits"
             % (p["hold_days"], p["exit_by"])]
    e = p.get("earnings") or {}
    if e.get("date"):
        lines.append("  Earnings    %s (%s)%s" % (e["date"], e["source"],
                                                 ": INSIDE the hold" if e.get("inside") else ": outside the hold"))
    for w in p.get("warnings", []):
        lines.append("  ! " + w)
    lines.append("  " + p["note"])
    lines.append("Next: swing.py replay (the multi-day Time Machine), then trader-budget sizes it as a "
                 "good-till-canceled bracket.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- the multi-day Time Machine

def _walk(entry, stop, target, days):
    """days: [(open, high, low, close)] -> (outcome, exit price, day number)."""
    for n, (o, h, l, c) in enumerate(days, 1):
        if o <= stop:
            return "stop", o, n            # gapped through the stop: fills at the open
        if o >= target:
            return "target", o, n
        if l <= stop:
            return "stop", stop, n         # if both hit the same day, count the stop (we cannot see the order)
        if h >= target:
            return "target", target, n
    return "time", days[-1][3], len(days)


def replay(plan, bars, n=10000, seed=19, lookback=250):
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    hold = int(plan["hold_days"])
    risk = entry - stop
    hist = bars[-(lookback + 1):]
    moves = []
    for prev, b in zip(hist, hist[1:]):
        pc = prev["close"]
        moves.append((b["open"] / pc, b["high"] / pc, b["low"] / pc, b["close"] / pc))
    rng = random.Random(seed)
    counts = {"target": 0, "stop": 0, "time": 0}
    rs, closes = [], []
    for _ in range(n):
        px = entry
        days = []
        for _d in range(hold):
            o, h, l, c = moves[rng.randrange(len(moves))]
            days.append((px * o, px * h, px * l, px * c))
            px = px * c
        out, fill, _day = _walk(entry, stop, target, days)
        counts[out] += 1
        rs.append((fill - entry) / risk)
        closes.append(px)
    sim = {k: round(100.0 * v / n, 1) for k, v in counts.items()}
    sim["avg_r"] = round(sum(rs) / n, 2)
    sim["runs"] = n
    sim["seed"] = seed

    # the same distances from every past day
    stop_pct, tgt_pct = stop / entry, target / entry
    hcount = {"target": 0, "stop": 0, "time": 0}
    hrs = []
    for i in range(1, len(bars) - hold):
        e = bars[i]["close"]
        s, t = e * stop_pct, e * tgt_pct
        days = [(b["open"], b["high"], b["low"], b["close"]) for b in bars[i + 1:i + 1 + hold]]
        out, fill, _ = _walk(e, s, t, days)
        hcount[out] += 1
        hrs.append((fill - e) / (e - s))
    total = sum(hcount.values())
    hist_res = {"days": total, "from": bars[1]["date"], "to": bars[len(bars) - hold - 1]["date"]}
    hist_res.update(hcount)
    hist_res["avg_r"] = round(sum(hrs) / total, 2) if total else None
    verdict = verdict_for(sim["avg_r"], hist_res["avg_r"])
    return {"simulated": sim, "history": hist_res, "verdict": verdict, "lookback_days": len(moves)}


def verdict_for(sim_r, hist_r):
    rs = [r for r in (sim_r, hist_r) if r is not None]
    worst = min(rs)
    if worst <= -0.1:
        return "negative: on this stock's own recent days these distances lost money on average. Skip it."
    if worst < 0.1:
        return ("close to a coin flip: no edge from the distances alone, and costs and slippage make it "
                "slightly negative. Only your setup (why this stock, why now) could make it worth taking.")
    return ("positive on average (%.2fR), from the stock's own recent drift. Recent drift does not have to "
            "continue: size it as if it were a coin flip." % worst)


def render_replay(plan, r):
    s, h = r["simulated"], r["history"]
    lines = ["TIME MACHINE (multi-day) %s: entry %s, stop %s, target %s, up to %d trading days"
             % (plan["symbol"], p2(plan["entry"]), p2(plan["stop"]), p2(plan["target"]), plan["hold_days"]),
             "  %d simulated holds, each day drawn from %s's own last %d daily bars (gaps included, seed %d):"
             % (s["runs"], plan["symbol"], r["lookback_days"], s["seed"]),
             "    target first %5.1f%%   stop first %5.1f%%   neither by the time limit %5.1f%%   average %+.2fR"
             % (s["target"], s["stop"], s["time"], s["avg_r"]),
             "  The same stop and target distances from every one of the last %d days (%s to %s):"
             % (h["days"], h["from"], h["to"]),
             "    target first %d   stop first %d   neither %d   average %+.2fR"
             % (h["target"], h["stop"], h["time"], h["avg_r"] or 0),
             "  Verdict: " + r["verdict"]]
    e = plan.get("earnings") or {}
    if e.get("inside"):
        lines.append("  ! Earnings likely inside this hold (%s). These odds do not include an earnings gap." % e["date"])
    lines.append("  A simulation, not a prediction: it only knows the last year of this stock.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def cmd_plan(a):
    cfg = read_json(a.config, required=False) or {}
    bars = load_bars(a.symbol, a.offline)
    day = datetime.date.fromisoformat(a.today) if a.today else ny_today()
    hold = int(a.hold if a.hold is not None else (cfg.get("swing_hold_days") or DEFAULTS["swing_hold_days"]))
    earn = None
    if not a.no_earnings:
        earn = earnings_check(a.symbol, day, add_trading_days(day, hold), a.earnings,
                              sec_user_agent(a.config))
    p = make_plan(a.symbol, bars, cfg, a.entry, a.stop, a.atr_mult, a.target, hold, day, earn)
    print(json.dumps(p, indent=2) if a.json else render_plan(p))
    if a.write:
        write_json(a.out, p)
        print("Wrote %s." % a.out)
    return 0


def cmd_replay(a):
    plan = read_json(a.plan, what="plan (run swing.py plan --write)")
    if plan.get("style") != "swing":
        raise SwingError("%s is not a swing plan; for a day trade use trader-timemachine" % a.plan)
    bars = load_bars(plan["symbol"], a.offline)
    r = replay(plan, bars, n=a.runs, seed=a.seed)
    print(json.dumps(r, indent=2) if a.json else render_replay(plan, r))
    if a.write:
        plan["replay"] = r
        write_json(a.plan, plan)
    return 0


def cmd_earnings(a):
    day = ny_today()
    e = earnings_check(a.symbol, day, add_trading_days(day, 15), None, sec_user_agent(a.config))
    if e.get("date"):
        print("%s next earnings around %s (%s)." % (a.symbol.upper(), e["date"], e["source"]))
        return 0
    print(e.get("note", "unknown"))
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing trades (days to weeks): plan, multi-day replay, earnings.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan")
    p.add_argument("symbol")
    p.add_argument("--entry", type=float, help="entry price (default: the last daily close)")
    p.add_argument("--stop", help="atr (default), low10, a percent like 6%%, or a price")
    p.add_argument("--atr-mult", type=float, help="ATR multiple for --stop atr (default 2)")
    p.add_argument("--target", help="2R (default), 1.5R, or a price")
    p.add_argument("--hold", type=int, help="max trading days to hold (default 15)")
    p.add_argument("--earnings", help="the next earnings date if you know it, YYYY-MM-DD")
    p.add_argument("--no-earnings", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--today", help=argparse.SUPPRESS)
    p.add_argument("--offline", help="a saved Yahoo daily chart JSON")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--out", default="trader/plan.json")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("replay")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--runs", type=int, default=10000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--offline")
    p.add_argument("--write", action="store_true", help="store the result in the plan")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("earnings")
    p.add_argument("symbol")
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "replay": cmd_replay, "earnings": cmd_earnings}[a.cmd](a)
    except SwingError as e:
        print("swing.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
