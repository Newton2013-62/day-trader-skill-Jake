#!/usr/bin/env python3
"""Backtest the swing hunt's rules over years of daily bars. Standard library only.

The question it answers: if you had taken every swing setup the scanner finds,
with the stop, target and time limit you use now, did it make money, how
bumpy was the ride, and did it beat just holding SPY?

The rules (the same ones the hunt uses):
  signal   a daily setup from trader-scanner on day D's close (20-day closing-high
           breakout, bull flag, reclaimed 20-day average, oversold, oversold bounce)
  filters  price >= $3, 20-day average dollar volume >= $3M, 14-day ATR <= 6% of price
  entry    the NEXT day's open (you see it in the evening, buy the next morning)
  stop     entry - 2 x the 14-day ATR        (config swing_atr_mult)
  target   entry + 3R                         (config swing_target_r)
  exit     stop or target, whichever first (a gap fills at the open), else the
           close after N trading days        (config swing_hold_days)
  costs    0.1% slippage each way (--slippage)

Then it runs four variants side by side: every signal; only when the market
regime is risk-on (SPY above its 200- and rising 50-day); only stocks beating
SPY (relative strength above 0); and both filters together. Each variant is
also run as a real account: your budget, max positions, $ risk per trade,
max order and open-risk cap from trader/config.json, best relative strength
first when there are more signals than room, marked to market every day.

    python3 backtest.py --sample 500             # 500 random stocks from trader/universe.txt
    python3 backtest.py --all                    # the whole universe (slow the first time)
    python3 backtest.py --symbols AAPL,NVDA,SOFI --years 3
    python3 backtest.py --setups breakout_20d,bull_flag --json

Bars are cached in trader/cache/daily, so a re-run the same day is fast.

The big caveat, said every time: the universe is TODAY's listed stocks, so
companies that went bankrupt or were delisted in the last five years are
missing. That makes every backtest like this look better than reality.
Education and tooling, not financial advice.
"""

import argparse
import datetime
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


edge = _load(os.path.join(HERE, "edge.py"), "trader_edge")
scan = _load(os.path.join(os.path.dirname(HERE), "trader-scanner", "scan.py"), "trader_scan_bt")

DEFAULTS = {"budget": 3000, "cash_buffer_pct": 10, "max_position_pct": 35, "max_order_usd": 1000,
            "max_loss_per_trade_usd": 50, "max_open_positions": 4, "max_open_risk_usd": 200,
            "swing_hold_days": 15, "swing_atr_mult": 2.0, "swing_target_r": 2.0}
SCANNER_SETUPS = [k for k, _ in scan.SETUPS]
EXTRA_SETUPS = ["capitulation", "breakout_6m", "sr_breakout"]   # Lance's two setups + horizontal resistance breakout
ALL_SETUPS = SCANNER_SETUPS + EXTRA_SETUPS
EXTRA_LABEL = {"capitulation": "capitulation reversal (volume flush, then the turn)",
               "breakout_6m": "multi-month breakout on volume",
               "sr_breakout": "support/resistance breakout (2+ touches, volume)"}
VARIANTS = [("all", "every signal"), ("regime", "risk-on market only"),
            ("rs", "beating SPY only"), ("both", "risk-on AND beating SPY")]
WARMUP = 220      # bars before the first signal: 200-day average + slope + ATR


class BacktestError(Exception):
    pass


# ---------------------------------------------------------------- pure pieces

def atr_at(bars, i, n=14):
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        b, pc = bars[k], bars[k - 1]["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return sum(trs) / n


def walk(entry, stop, target, days):
    """days: [(open, high, low, close)] from the entry day on -> (outcome, exit price, days held).
    The entry day's open IS the entry, so a gap is only possible from day 2."""
    for n, (o, h, l, c) in enumerate(days, 1):
        if n > 1 and o <= stop:
            return "stop", o, n
        if n > 1 and o >= target:
            return "target", o, n
        if l <= stop:
            return "stop", stop, n      # both inside one bar: count the stop
        if h >= target:
            return "target", target, n
    return "time", days[-1][3], len(days)


def walk_ma(entry, stop, target, bars, start, hold, n, closes):
    """Like walk, but also sells at the close on the first day the close is above its n-day
    average (the classic exit for a pullback trade). -> (outcome, exit price, days held)."""
    last = min(len(bars), start + hold)
    for k in range(start, last):
        o, h, l, c = bars[k]["open"], bars[k]["high"], bars[k]["low"], bars[k]["close"]
        first = k == start
        if not first and o <= stop:
            return "stop", o, k - start + 1
        if not first and o >= target:
            return "target", o, k - start + 1
        if l <= stop:
            return "stop", stop, k - start + 1
        if h >= target:
            return "target", target, k - start + 1
        if k + 1 >= n and c > sum(closes[k - n + 1:k + 1]) / n:
            return "bounce", c, k - start + 1
    return "time", bars[last - 1]["close"], last - start


def rsi_series(closes, n=14):
    """Wilder's RSI for every bar (None until n+1 bars). The scanner computes it on a
    shorter window; after ~100 bars the two agree to a fraction of a point."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def setups_at(c, i, rsi):
    """The scanner's daily setups (scan.find_setups) on bar i, with a precomputed RSI. Pure."""
    if i < 20:
        return []
    last = c[i]
    hi20 = max(c[i - 20:i])
    ma20 = sum(c[i - 19:i + 1]) / 20.0
    ma20_prev = sum(c[i - 20:i]) / 20.0
    run = c[i - 5] / c[i - 15] - 1
    w5 = c[i - 4:i + 1]
    rng5 = (max(w5) - min(w5)) / last
    r, r_prev = rsi[i], rsi[i - 1]
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[i - 1] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[i - 1]:
        tags.append("oversold_bounce")
    return tags


def lance_setups(bars, closes, i, a, wanted):
    """Lance Breitstein's two daily swing setups on bar i. Pure.
    -> [(tag, structural stop price)].

    capitulation: in the last 10 days there was a flush day k: the close is at least
      max(15%, 4 ATR) under the 20-day high, it fell three days in a row with day k's
      drop the biggest, on at least 2.5x the 50-day average volume. Today (i > k) the
      stock turns: it closes above the prior day's high ("the right side of the V").
      Stop: a cent under the lowest low since the flush.
    breakout_6m: today closes above every close of the last 126 days (about 6 months),
      after at least 60 days with a range under 35% (a base), on at least 1.5x the
      50-day average volume. Stop: a cent under today's low."""
    out = []
    if i < 130:
        return out
    vol50 = sum(b["volume"] for b in bars[i - 50:i]) / 50.0
    if "capitulation" in wanted and vol50 > 0 and closes[i] > bars[i - 1]["high"]:
        for k in range(i - 1, max(i - 11, 25), -1):
            hi20 = max(b["high"] for b in bars[k - 20:k + 1])
            ck = closes[k]
            need = max(0.15, 4 * a / ck)
            d0 = closes[k] - closes[k - 1]
            d1 = closes[k - 1] - closes[k - 2]
            d2 = closes[k - 2] - closes[k - 3]
            if (ck <= hi20 * (1 - need) and d0 < 0 and d1 < 0 and d2 < 0 and d0 <= d1 and d0 <= d2
                    and bars[k]["volume"] >= 2.5 * vol50):
                low = min(b["low"] for b in bars[k:i + 1])
                out.append(("capitulation", low - 0.01))
                break
    if "breakout_6m" in wanted and vol50 > 0:
        prior = closes[i - 126:i]
        base = closes[i - 60:i]
        if (closes[i] > max(prior) and (max(base) - min(base)) / max(base) < 0.35
                and bars[i]["volume"] >= 1.5 * vol50):
            out.append(("breakout_6m", bars[i]["low"] - 0.01))
    return out


def resistance_level(bars, closes, i, lookback=120, pivot=3, tol=0.015, touches=2, gap=10):
    """The horizontal resistance that bar i just broke, or None. Pure.
    A swing high is a bar whose high is above the `pivot` bars before it and not
    below the `pivot` bars after it.
    A level is a swing high with at least `touches` swing highs (itself included,
    each `gap` days apart) within `tol` of it in the last `lookback` days, and no
    close above it from its first touch until yesterday. Bar i must close above the
    level after yesterday closed at or under it. Picks the highest such level."""
    lo = max(pivot, i - lookback)
    piv = []
    for j in range(lo, i - pivot):
        h = bars[j]["high"]
        if (all(h > bars[k]["high"] for k in range(j - pivot, j))
                and all(h >= bars[k]["high"] for k in range(j + 1, j + pivot + 1))):
            piv.append((j, h))
    best = None
    for j, lvl in piv:
        near = [k for k, h in piv if abs(h - lvl) <= tol * lvl]
        spaced = [near[0]] if near else []
        for k in near[1:]:
            if k - spaced[-1] >= gap:
                spaced.append(k)
        if len(spaced) < touches:
            continue
        top = max(h for k, h in piv if k in near)
        if max(closes[near[0]:i]) > top:
            continue
        if closes[i] > top and closes[i - 1] <= top and (best is None or top > best):
            best = top
    return best


def sr_setup(bars, closes, i, a, vol_mult=1.5, max_ext_atr=1.0):
    """Support/resistance breakout on bar i. Pure. -> stop price or None.
    Close breaks a resistance level (resistance_level) on at least `vol_mult` x the
    50-day average volume, and not more than `max_ext_atr` ATRs past it (no chasing).
    Stop: half an ATR under the old resistance (it should now act as support)."""
    if i < 130 or not a:
        return None
    vol50 = sum(b["volume"] for b in bars[i - 50:i]) / 50.0
    if vol50 <= 0 or bars[i]["volume"] < vol_mult * vol50:
        return None
    lvl = resistance_level(bars, closes, i)
    if lvl is None or closes[i] - lvl > max_ext_atr * a:
        return None
    return round(lvl - 0.5 * a, 2)


def walk_trail(entry, stop, target, bars, start, hold, mode, closes, risk):
    """Exit with a trailing stop. Pure. -> (outcome, exit price, days held).
    mode "low": once a close is at least 1R above the entry, the stop rises to each
      prior day's low (never down). mode "ma20": sell at the close once it closes under
      its 20-day average. target None = no target, let it run up to `hold` days."""
    last = min(len(bars), start + hold)
    trailing = False
    for k in range(start, last):
        o, h, l, c = bars[k]["open"], bars[k]["high"], bars[k]["low"], bars[k]["close"]
        first = k == start
        if not first and o <= stop:
            return ("trail" if trailing else "stop"), o, k - start + 1
        if target and not first and o >= target:
            return "target", o, k - start + 1
        if l <= stop:
            return ("trail" if trailing else "stop"), stop, k - start + 1
        if target and h >= target:
            return "target", target, k - start + 1
        if mode == "ma20" and k >= 19 and c < sum(closes[k - 19:k + 1]) / 20.0:
            return "ma20", c, k - start + 1
        if mode == "low":
            if c >= entry + risk:
                trailing = True
            if trailing and l > stop:
                stop = l
    return "time", bars[last - 1]["close"], last - start


def signals_for(sym, bars, spy_close_by_date, regime_by_date, rules):
    """Every trade the rules would have taken in one stock, one at a time (no
    new entry while the last one is open). Pure apart from the inputs."""
    closes = [b["close"] for b in bars]
    rsi = rsi_series(closes)
    out = []
    i = WARMUP if len(bars) > WARMUP + 2 else 60
    hold = rules["hold"]
    busy_until = -1
    spy_dates = spy_close_by_date
    while i < len(bars) - 1:
        if i <= busy_until:
            i += 1
            continue
        b = bars[i]
        c = b["close"]
        if c < rules["min_price"]:
            i += 1
            continue
        dv = sum(x["close"] * x["volume"] for x in bars[i - 19:i + 1]) / 20.0
        if dv < rules["min_dollar_volume"]:
            i += 1
            continue
        a = atr_at(bars, i)
        if not a or a / c * 100 > rules["max_atr_pct"]:
            i += 1
            continue
        ma_n = rules.get("above_ma") or 0
        if ma_n and (i + 1 < ma_n or c <= sum(closes[i - ma_n + 1:i + 1]) / ma_n):
            i += 1
            continue
        tags = [t for t in setups_at(closes, i, rsi) if t in rules["setups"]]
        struct = None
        extra = [x for x in rules["setups"] if x in EXTRA_SETUPS]
        if extra:
            for tag, st in lance_setups(bars, closes, i, a, extra):
                tags.append(tag)
                struct = st if struct is None else min(struct, st)
            if "sr_breakout" in extra:
                st = sr_setup(bars, closes, i, a)
                if st is not None:
                    tags.append("sr_breakout")
                    struct = st if struct is None else min(struct, st)
        if not tags:
            i += 1
            continue
        # relative strength on the signal day, against SPY on the same dates
        spy_aligned = [spy_dates.get(x["date"]) for x in bars[max(0, i - 130):i + 1]]
        own = closes[max(0, i - 130):i + 1]
        pairs = [(s, o) for s, o in zip(spy_aligned, own) if s is not None]
        rs = None
        if len(pairs) >= 64:
            rs, _ = edge.rs_score([o for _, o in pairs], [s for s, _ in pairs])
        e = bars[i + 1]
        entry = e["open"] * (1 + rules["slippage"])
        if rules.get("stop_mode") == "structure" and struct is not None and struct < entry:
            stop = struct
            risk = entry - stop
            if risk < 0.5 * a:          # a stop tighter than half a day's range is noise: widen it
                risk = 0.5 * a
                stop = entry - risk
        else:
            risk = rules["atr_mult"] * a
            stop = entry - risk
        target = entry + rules["target_r"] * risk if rules["target_r"] > 0 else None
        days = [(x["open"], x["high"], x["low"], x["close"]) for x in bars[i + 1:i + 1 + hold]]
        if not days or stop <= 0:
            i += 1
            continue
        if rules.get("trail") in ("low", "ma20"):
            outcome, fill, held = walk_trail(entry, stop, target, bars, i + 1, hold, rules["trail"], closes, risk)
        elif target is None:
            outcome, fill, held = walk(entry, stop, float("inf"), days)
        elif rules.get("exit_ma"):
            outcome, fill, held = walk_ma(entry, stop, target, bars, i + 1, hold, rules["exit_ma"], closes)
        else:
            outcome, fill, held = walk(entry, stop, target, days)
        exit_px = fill * (1 - rules["slippage"])
        r = (exit_px - entry) / risk
        exit_i = i + held          # the entry is bar i+1, so held days end at bar i+held
        out.append({"symbol": sym, "signal_date": b["date"], "entry_date": e["date"],
                    "exit_date": bars[exit_i]["date"], "setups": tags, "entry": entry, "stop": stop,
                    "target": target if target is not None else 0.0, "exit": exit_px, "outcome": outcome, "days": held,
                    "r": r, "rs": rs, "regime": regime_by_date.get(b["date"]),
                    "complete": outcome != "time" or held == hold})
        busy_until = exit_i
        i += 1
    return out


def keep(trade, variant):
    if variant == "all":
        return True
    reg_ok = trade["regime"] == "risk-on"
    rs_ok = trade["rs"] is not None and trade["rs"] > 0
    return {"regime": reg_ok, "rs": rs_ok, "both": reg_ok and rs_ok}[variant]


def trade_stats(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    gross_loss = -sum(losses)
    return {"trades": n, "win_pct": round(100.0 * len(wins) / n, 1), "avg_r": round(sum(rs) / n, 3),
            "total_r": round(sum(rs), 1),
            "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else None,
            "target_pct": round(100.0 * sum(1 for t in trades if t["outcome"] == "target") / n, 1),
            "stop_pct": round(100.0 * sum(1 for t in trades if t["outcome"] == "stop") / n, 1),
            "time_pct": round(100.0 * sum(1 for t in trades if t["outcome"] == "time") / n, 1),
            "avg_days": round(sum(t["days"] for t in trades) / n, 1),
            "worst_losing_streak": worst}


def account(trades, closes_by_sym, dates, cfg):
    """Run the trades through one account with the config's limits. Pure.
    trades: candidate trades sorted any way; dates: every trading date in order.
    Returns the equity curve and the summary."""
    budget = float(cfg["budget"])
    cash = budget
    buffer = budget * float(cfg["cash_buffer_pct"]) / 100.0
    pos_cap = budget * float(cfg["max_position_pct"]) / 100.0
    max_order = float(cfg["max_order_usd"])
    risk_usd = float(cfg["max_loss_per_trade_usd"])
    max_pos = int(cfg["max_open_positions"])
    max_open_risk = float(cfg["max_open_risk_usd"])
    by_entry = {}
    for t in trades:
        by_entry.setdefault(t["entry_date"], []).append(t)
    open_pos = []
    taken, skipped = [], 0
    curve = []
    peak = budget
    max_dd = 0.0
    max_dd_pct = 0.0
    last_close = {}
    for d in dates:
        # exits first: a slot freed today can be used by today's entries
        still = []
        for p in open_pos:
            if p["t"]["exit_date"] <= d:
                cash += p["shares"] * p["t"]["exit"]
                p["t"]["pnl"] = round(p["shares"] * (p["t"]["exit"] - p["t"]["entry"]), 2)
            else:
                still.append(p)
        open_pos = still
        for t in sorted(by_entry.get(d, []), key=lambda t: -(t["rs"] if t["rs"] is not None else -1e9)):
            if len(open_pos) >= max_pos or any(p["t"]["symbol"] == t["symbol"] for p in open_pos):
                skipped += 1
                continue
            risk_ps = t["entry"] - t["stop"]
            open_risk = sum(p["shares"] * (p["t"]["entry"] - p["t"]["stop"]) for p in open_pos)
            room_risk = min(risk_usd, max_open_risk - open_risk)
            spend_cap = min(max_order, pos_cap, cash - buffer)
            shares = int(min(room_risk / risk_ps if risk_ps > 0 else 0, spend_cap / t["entry"]))
            if shares < 1:
                skipped += 1
                continue
            cash -= shares * t["entry"]
            t = dict(t, shares=shares, risk_usd=round(shares * risk_ps, 2))
            open_pos.append({"t": t, "shares": shares})
            taken.append(t)
        value = cash
        for p in open_pos:
            sym = p["t"]["symbol"]
            px = closes_by_sym.get(sym, {}).get(d)
            if px is not None:
                last_close[sym] = px
            value += p["shares"] * last_close.get(sym, p["t"]["entry"])
        curve.append((d, round(value, 2)))
        peak = max(peak, value)
        if peak - value > max_dd:
            max_dd = peak - value
        max_dd_pct = max(max_dd_pct, (peak - value) / peak * 100 if peak else 0)
    for p in open_pos:     # still open at the end: valued at the last close
        p["t"]["pnl"] = round(p["shares"] * (last_close.get(p["t"]["symbol"], p["t"]["entry"]) - p["t"]["entry"]), 2)
    end = curve[-1][1] if curve else budget
    years = max(1e-9, len(dates) / 252.0)
    return {"start": budget, "end": round(end, 2), "return_pct": round((end / budget - 1) * 100, 1),
            "cagr_pct": round(((end / budget) ** (1 / years) - 1) * 100, 1) if end > 0 else -100.0,
            "max_drawdown_usd": round(max_dd, 2), "max_drawdown_pct": round(max_dd_pct, 1),
            "taken": len(taken), "skipped_no_room": skipped,
            "stats": trade_stats(taken), "trades": taken}, curve


def buy_and_hold(spy_bars, dates, budget):
    by = {b["date"]: b["close"] for b in spy_bars}
    px = [by[d] for d in dates if d in by]
    if len(px) < 2:
        return None
    peak, dd = px[0], 0.0
    for p in px:
        peak = max(peak, p)
        dd = max(dd, (peak - p) / peak * 100)
    years = len(dates) / 252.0
    end = budget * px[-1] / px[0]
    return {"end": round(end, 2), "return_pct": round((px[-1] / px[0] - 1) * 100, 1),
            "cagr_pct": round(((px[-1] / px[0]) ** (1 / years) - 1) * 100, 1),
            "max_drawdown_pct": round(dd, 1)}


def by_key(trades, keyfn):
    groups = {}
    for t in trades:
        for k in keyfn(t):
            groups.setdefault(k, []).append(t)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------- run

def pick_symbols(a):
    if a.symbols:
        return [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    path = a.universe
    if not os.path.exists(path):
        raise BacktestError("no %s: run `python trader-scanner/scan.py universe` first, or pass --symbols" % path)
    syms = scan.symbols_from_file(path)
    if a.all:
        return syms
    rng = random.Random(a.seed)
    return sorted(rng.sample(syms, min(a.sample, len(syms))))


def run(a):
    cfg = dict(DEFAULTS)
    cfg.update(edge.read_json(a.config, required=False) or {})
    rules = {"min_price": a.min_price, "min_dollar_volume": a.min_dollar_volume, "max_atr_pct": a.max_atr_pct,
             "hold": int(a.hold or cfg["swing_hold_days"]), "atr_mult": float(a.atr_mult or cfg["swing_atr_mult"]),
             "target_r": float(a.target_r if a.target_r is not None else cfg["swing_target_r"]), "slippage": a.slippage / 100.0,
             "setups": [s for s in (a.setups.split(",") if a.setups else SCANNER_SETUPS) if s],
             "above_ma": a.above_ma, "exit_ma": a.exit_ma, "stop_mode": a.stop_mode, "trail": a.trail}
    bad = [s for s in rules["setups"] if s not in ALL_SETUPS]
    if bad:
        raise BacktestError("unknown setup %s; use %s" % (", ".join(bad), ", ".join(ALL_SETUPS)))
    # about one more year than reported, so the 200-day average is ready on day one
    fetch_rng = "2y" if a.years <= 1 else ("5y" if a.years <= 4 else "10y")
    spy = edge.cached_bars("SPY", fetch_rng, a.cache, max_age_hours=a.cache_hours, min_bars=300)
    first_day = (datetime.date.fromisoformat(spy[-1]["date"]) - datetime.timedelta(days=int(365.25 * a.years))).isoformat()
    spy_close = {b["date"]: b["close"] for b in spy}
    regime = edge.regime_series(spy)
    syms = pick_symbols(a)
    log = (lambda m: None) if a.json else (lambda m: print(m, file=sys.stderr))
    stop_txt = "structural stop" if rules.get("stop_mode") == "structure" else "stop %gx ATR" % rules["atr_mult"]
    tgt_txt = "target %gR" % rules["target_r"] if rules["target_r"] > 0 else "no target"
    trail_txt = "" if rules.get("trail") in (None, "none") else ", trail %s" % rules["trail"]
    log("Backtesting %d stocks over %d years: entry next open, %s, %s%s, hold up to %d days..."
        % (len(syms), a.years, stop_txt, tgt_txt, trail_txt, rules["hold"]))
    trades, closes_by_sym, failed = [], {}, {}
    t0 = time.time()
    for k, sym in enumerate(syms):
        try:
            path_cached = os.path.exists(os.path.join(a.cache, "%s_%s.json.gz" % (sym, fetch_rng)))
            bars = edge.cached_bars(sym, fetch_rng, a.cache, max_age_hours=a.cache_hours, min_bars=60)
            if not path_cached and a.pause:
                edge.SLEEP(a.pause)
        except edge.EdgeError as e:
            failed[sym] = str(e)
            continue
        got = [t for t in signals_for(sym, bars, spy_close, regime, rules) if t["entry_date"] >= first_day]
        if got:
            trades.extend(got)
            closes_by_sym[sym] = {b["date"]: b["close"] for b in bars if b["date"] >= first_day}
        if (k + 1) % 50 == 0:
            log("  %d of %d stocks, %d trades so far (%ds)" % (k + 1, len(syms), len(trades), time.time() - t0))
    dates = [b["date"] for b in spy if b["date"] >= first_day]
    trades.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    result = {"created_by": "trader-swing/backtest", "from": dates[0] if dates else None,
              "to": dates[-1] if dates else None, "stocks": len(syms), "no_data": len(failed),
              "rules": rules, "account": {k: cfg[k] for k in DEFAULTS if k in cfg}, "variants": {}}
    for key, label in VARIANTS:
        vt = [t for t in trades if keep(t, key)]
        acct, curve = account([dict(t) for t in vt], closes_by_sym, dates, cfg)
        result["variants"][key] = {
            "label": label, "every_trade": trade_stats(vt), "account": {k: v for k, v in acct.items() if k != "trades"},
            "by_year": by_key(vt, lambda t: [t["entry_date"][:4]]),
            "by_setup": by_key(vt, lambda t: t["setups"]),
            "curve": curve[::5] + ([curve[-1]] if curve and len(curve) % 5 != 1 else []),
        }
    result["spy_buy_and_hold"] = buy_and_hold(spy, dates, float(cfg["budget"]))
    result["regime_days"] = {s: sum(1 for d in dates if regime.get(d) == s) for s in ("risk-on", "caution", "risk-off")}
    result["seconds"] = round(time.time() - t0, 1)
    return result


def _pct(x):
    return "-" if x is None else "%.1f%%" % x


def render(res):
    L = []
    r = res["rules"]
    L.append("SWING BACKTEST %s to %s, %d stocks (%d had no data)" % (res["from"], res["to"], res["stocks"], res["no_data"]))
    stop_txt = "structural stop" if r.get("stop_mode") == "structure" else "stop %gx ATR" % r["atr_mult"]
    tgt_txt = "target %gR" % r["target_r"] if r["target_r"] > 0 else "no target"
    L.append("Rules: setups %s; price >= $%g, 20-day dollar volume >= $%s, ATR <= %g%%; entry next open, %s, "
             "%s, out after %d days; %.2f%% slippage each way."
             % (", ".join(r["setups"]), r["min_price"], "{:,.0f}".format(r["min_dollar_volume"]), r["max_atr_pct"],
                stop_txt, tgt_txt, r["hold"], r["slippage"] * 100))
    if r.get("stop_mode") == "structure" or r.get("trail") not in (None, "none"):
        L.append("Exits: %s stop%s%s." % ("structural (the setup's own low)" if r.get("stop_mode") == "structure" else "ATR",
                                          {"low": "; trailing to the prior day's low once 1R up",
                                           "ma20": "; sell on a close under the 20-day average"}.get(r.get("trail"), ""),
                                          "; no fixed target" if not r["target_r"] else ""))
    if r.get("above_ma") or r.get("exit_ma"):
        L.append("Extra rules:%s%s" % (" only stocks above their %d-day average;" % r["above_ma"] if r.get("above_ma") else "",
                                       " also sell at the close once it closes above its %d-day average." % r["exit_ma"]
                                       if r.get("exit_ma") else ""))
    rd = res["regime_days"]
    tot = sum(rd.values()) or 1
    L.append("Market regime over the period: risk-on %d%% of days, caution %d%%, risk-off %d%%."
             % (100 * rd["risk-on"] / tot, 100 * rd["caution"] / tot, 100 * rd["risk-off"] / tot))
    L.append("")
    L.append("EVERY TRADE THE RULES FOUND (no account limits, 1 trade = 1R risked)")
    L.append("  %-26s %7s %6s %8s %8s %7s %7s %7s" % ("variant", "trades", "win%", "avg R", "total R", "PF", "streak", "days"))
    for key, v in res["variants"].items():
        s = v["every_trade"]
        if not s.get("trades"):
            L.append("  %-26s %7d" % (v["label"], 0))
            continue
        L.append("  %-26s %7d %5.1f%% %+8.3f %+8.1f %7s %7d %7.1f" % (
            v["label"], s["trades"], s["win_pct"], s["avg_r"], s["total_r"],
            "-" if s["profit_factor"] is None else "%.2f" % s["profit_factor"], s["worst_losing_streak"], s["avg_days"]))
    L.append("")
    acc = res["account"]
    L.append("AS YOUR ACCOUNT ($%s start, $%s max loss a trade, %s positions, $%s open risk, $%s max order; "
             "best relative strength first)" % ("{:,.0f}".format(acc["budget"]), "{:,.0f}".format(acc["max_loss_per_trade_usd"]),
                                                acc["max_open_positions"], "{:,.0f}".format(acc["max_open_risk_usd"]),
                                                "{:,.0f}".format(acc["max_order_usd"])))
    L.append("  %-26s %10s %8s %7s %12s %7s %7s" % ("variant", "end", "return", "CAGR", "worst drop", "taken", "avg R"))
    for key, v in res["variants"].items():
        a = v["account"]
        s = a["stats"]
        L.append("  %-26s %10s %7s %7s %6s %5s %7d %+7.3f" % (
            v["label"], "${:,.0f}".format(a["end"]), _pct(a["return_pct"]), _pct(a["cagr_pct"]),
            "${:,.0f}".format(a["max_drawdown_usd"]), "(%s)" % _pct(a["max_drawdown_pct"]), a["taken"], s.get("avg_r", 0) or 0))
    b = res["spy_buy_and_hold"]
    if b:
        L.append("  %-26s %10s %7s %7s %12s" % ("just holding SPY", "${:,.0f}".format(b["end"]), _pct(b["return_pct"]),
                                              _pct(b["cagr_pct"]), "(%s)" % _pct(b["max_drawdown_pct"])))
    best = res["variants"]["both"]
    L.append("")
    L.append("BY YEAR (risk-on AND beating SPY, every trade)")
    for y, s in best["by_year"].items():
        L.append("  %s  %5d trades  win %5.1f%%  avg %+.3fR  total %+7.1fR" % (y, s["trades"], s["win_pct"], s["avg_r"], s["total_r"]))
    L.append("BY SETUP (every signal)")
    for k, s in res["variants"]["all"]["by_setup"].items():
        L.append("  %-30s %5d trades  win %5.1f%%  avg %+.3fR" % (scan.LABEL.get(k, EXTRA_LABEL.get(k, k)), s["trades"], s["win_pct"], s["avg_r"]))
    L.append("")
    L.append(verdict(res))
    L.append("Caveats: today's stock list only (delisted losers are missing, which flatters the result); free "
             "Yahoo data; daily bars cannot see whether the stop or the target came first inside one day "
             "(counted as the stop). A backtest is a history lesson, not a forecast.")
    L.append("Education and tooling, not financial advice.")
    return "\n".join(L)


def verdict(res):
    v = res["variants"]["both"]
    s = v["every_trade"]
    a = v["account"]
    b = res["spy_buy_and_hold"] or {}
    if not s.get("trades"):
        return "Verdict: no trades passed both filters, so there is nothing to judge."
    if s["trades"] < 100:
        size = "Only %d trades: too few to trust either way. " % s["trades"]
    else:
        size = ""
    if s["avg_r"] <= 0:
        return size + ("Verdict: with both filters the rules LOST money on average (%+.3fR a trade). As they stand they "
                       "have no edge; do not trade them with real money." % s["avg_r"])
    beat = b and a["cagr_pct"] is not None and a["cagr_pct"] > b.get("cagr_pct", 0)
    if not beat:
        return size + ("Verdict: the rules made money (%+.3fR a trade) but the account grew less than just holding SPY "
                       "(%s vs %s a year). Positive, but not worth the work and risk over an index fund yet."
                       % (s["avg_r"], _pct(a["cagr_pct"]), _pct(b.get("cagr_pct"))))
    return size + ("Verdict: the rules made money (%+.3fR a trade) and beat holding SPY (%s vs %s a year), with a worst "
                   "drop of %s. Promising, but prove it forward: journal every signal for a few months before trusting it."
                   % (s["avg_r"], _pct(a["cagr_pct"]), _pct(b.get("cagr_pct")), _pct(a["max_drawdown_pct"])))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the swing hunt's rules over years of daily bars.")
    ap.add_argument("--symbols", help="comma-separated tickers (instead of the universe)")
    ap.add_argument("--universe", default="trader/universe.txt")
    ap.add_argument("--sample", type=int, default=500, help="random stocks from the universe (default 500)")
    ap.add_argument("--all", action="store_true", help="every stock in the universe")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--years", type=int, default=5, help="1 to 9 (default 5)")
    ap.add_argument("--setups", help="comma list, default all: " + ",".join(ALL_SETUPS))
    ap.add_argument("--hold", type=int)
    ap.add_argument("--atr-mult", type=float)
    ap.add_argument("--target-r", type=float)
    ap.add_argument("--min-price", type=float, default=3.0)
    ap.add_argument("--min-dollar-volume", type=float, default=3e6)
    ap.add_argument("--max-atr-pct", type=float, default=6.0)
    ap.add_argument("--above-ma", type=int, default=0, help="only buy stocks closing above their N-day average (e.g. 200)")
    ap.add_argument("--stop-mode", choices=["atr", "structure"], default="atr",
                    help="atr (default) or structure: the setup's own low (capitulation / breakout_6m)")
    ap.add_argument("--trail", choices=["none", "low", "ma20"], default="none",
                    help="trailing exit: low (prior day's low once 1R up) or ma20 (close under the 20-day average)")
    ap.add_argument("--exit-ma", type=int, default=0, help="also sell at the close once it closes above its N-day average (e.g. 5)")
    ap.add_argument("--slippage", type=float, default=0.1, help="percent each way (default 0.1)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--cache", default="trader/cache/daily")
    ap.add_argument("--cache-hours", type=float, default=20)
    ap.add_argument("--pause", type=float, default=0.25)
    ap.add_argument("--out", default="trader/backtest.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not 1 <= a.years <= 9:
        print("backtest.py: --years wants 1 to 9", file=sys.stderr)
        return 1
    try:
        res = run(a)
    except (BacktestError, edge.EdgeError) as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1
    if a.out:
        edge.write_json(a.out, res)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(render(res))
        if a.out:
            print("Wrote %s." % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
