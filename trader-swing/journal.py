#!/usr/bin/env python3
"""The trade journal: log every swing idea, taken or not, and score it honestly. Standard library only.

The only way to know whether the hunt works is to write down every trade it
suggests, BEFORE you know how it ends, and count. This journal does that:

  add      log a plan (trader/plan.json by default, or flags) as suggested,
           taken or skipped, with the market regime and relative strength of
           the day it was found
  take     you bought it: shares and your fill price
  skip     you passed on it (it is still scored, so you learn what you missed)
  close    you sold it: your exit price and date
  update   for every idea still open, read the daily bars since it was found
           and see whether the stop, the target or the time limit came first
           (entry assumed at the NEXT day's open, like the backtest)
  list     the journal, newest first
  stats    win rate, average R, expectancy, by setup, by regime, taken vs not

    python3 journal.py add --status suggested             # from trader/plan.json
    python3 journal.py add --symbol SOFI --entry 14.20 --stop 13.10 --target 17.50 --hold 30 --setup bull_flag
    python3 journal.py take 3 --shares 40 --fill 14.25
    python3 journal.py close 3 --exit 17.48 --date 2026-12-18
    python3 journal.py update
    python3 journal.py stats

Writes trader/journal.json. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


edge = _load(os.path.join(HERE, "edge.py"), "trader_edge_j")
bt = _load(os.path.join(HERE, "backtest.py"), "trader_backtest_j")

MIN_TRADES = 30


class JournalError(Exception):
    pass


def today():
    return datetime.date.today().isoformat()


def load(path):
    doc = edge.read_json(path, required=False)
    return doc if isinstance(doc, dict) and "entries" in doc else {"created_by": "trader-swing/journal", "entries": []}


def save(path, doc):
    edge.write_json(path, doc)


def find(doc, eid):
    for e in doc["entries"]:
        if e["id"] == eid:
            return e
    raise JournalError("no journal entry #%d" % eid)


# ---------------------------------------------------------------- pure pieces

def new_entry(doc, plan, status, regime=None, rs=None, note=""):
    if status not in ("suggested", "taken", "skipped"):
        raise JournalError("status is suggested, taken or skipped")
    for k in ("symbol", "entry", "stop", "target"):
        if plan.get(k) in (None, ""):
            raise JournalError("the plan has no %s" % k)
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    if not stop < entry < target:
        raise JournalError("need stop < entry < target (got %s / %s / %s)" % (stop, entry, target))
    sym = str(plan["symbol"]).upper()
    day = plan.get("date") or today()
    for e in doc["entries"]:
        if e["symbol"] == sym and e["date"] == day and e["result"] is None:
            raise JournalError("%s is already in the journal for %s (#%d)" % (sym, day, e["id"]))
    eid = 1 + max([e["id"] for e in doc["entries"]] or [0])
    e = {"id": eid, "date": day, "symbol": sym, "setup": plan.get("setup") or "swing",
         "planned_entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
         "hold_days": int(plan.get("hold_days") or 30), "status": status,
         "regime": regime, "rs": rs, "note": note,
         "shares": None, "fill": None, "exit": None, "exit_date": None,
         "result": None}
    doc["entries"].append(e)
    return e


def resolve(e, bars):
    """Score one entry from daily bars (oldest first, with "date"). Pure.
    Entry = the open of the first bar AFTER the plan date (or your fill, if you took it).
    -> result dict, or None if it is still running."""
    after = [b for b in bars if b["date"] > e["date"]]
    if not after:
        return None
    if e.get("exit") is not None and e.get("fill") is not None:
        risk = e["fill"] - e["stop"]
        return {"outcome": "closed by you", "entry": e["fill"], "exit": e["exit"],
                "exit_date": e.get("exit_date"), "r": round((e["exit"] - e["fill"]) / risk, 2) if risk > 0 else None,
                "source": "your fills"}
    entry = float(e["fill"] if e.get("fill") is not None else after[0]["open"])
    stop, target = float(e["stop"]), float(e["target"])
    if entry <= stop:
        return {"outcome": "gapped under the stop before entry", "entry": round(entry, 2), "exit": None,
                "exit_date": after[0]["date"], "r": 0.0, "source": "daily bars"}
    days = [(b["open"], b["high"], b["low"], b["close"]) for b in after[:e["hold_days"]]]
    out, fill, held = bt.walk(entry, stop, target, days)
    if out == "time" and len(days) < e["hold_days"]:
        return None
    risk = entry - stop
    return {"outcome": out, "entry": round(entry, 2), "exit": round(fill, 2), "exit_date": after[held - 1]["date"],
            "r": round((fill - entry) / risk, 2), "source": "daily bars"}


def stats(entries):
    done = [e for e in entries if e["result"] and e["result"].get("r") is not None]

    def block(rows):
        n = len(rows)
        if not n:
            return None
        rs = [e["result"]["r"] for e in rows]
        wins = [r for r in rs if r > 0]
        loss = -sum(r for r in rs if r <= 0)
        return {"n": n, "win_pct": round(100.0 * len(wins) / n, 1), "avg_r": round(sum(rs) / n, 2),
                "total_r": round(sum(rs), 1), "profit_factor": round(sum(wins) / loss, 2) if loss else None}

    out = {"resolved": len(done), "open": len(entries) - len(done), "all": block(done),
           "taken": block([e for e in done if e["status"] == "taken"]),
           "not_taken": block([e for e in done if e["status"] != "taken"]),
           "by_setup": {}, "by_regime": {}}
    for key, field in (("by_setup", "setup"), ("by_regime", "regime")):
        groups = {}
        for e in done:
            groups.setdefault(str(e.get(field) or "unknown"), []).append(e)
        out[key] = {k: block(v) for k, v in sorted(groups.items())}
    pnl = [e["shares"] * (e["result"]["exit"] - e["result"]["entry"]) for e in done
           if e["status"] == "taken" and e.get("shares") and e["result"].get("exit") is not None]
    out["taken_pnl_usd"] = round(sum(pnl), 2) if pnl else None
    return out


# ---------------------------------------------------------------- commands

def cmd_add(a):
    doc = load(a.journal)
    if a.symbol:
        plan = {"symbol": a.symbol, "entry": a.entry, "stop": a.stop, "target": a.target,
                "hold_days": a.hold, "setup": a.setup, "date": a.date}
    else:
        plan = edge.read_json(a.plan)
        if a.setup:
            plan["setup"] = a.setup
        if a.date:
            plan["date"] = a.date
    reg = edge.read_json(a.regime, required=False) or {}
    regime = a.regime_state or reg.get("state")
    rs = a.rs if a.rs is not None else plan.get("rs")
    e = new_entry(doc, plan, a.status, regime, rs, a.note or "")
    save(a.journal, doc)
    print("Logged #%d %s %s: entry %.2f, stop %.2f, target %.2f, up to %d days (%s, market %s)."
          % (e["id"], e["date"], e["symbol"], e["planned_entry"], e["stop"], e["target"], e["hold_days"],
             e["status"], e["regime"] or "unknown"))
    return 0


def cmd_take(a):
    doc = load(a.journal)
    e = find(doc, a.id)
    e["status"], e["shares"], e["fill"] = "taken", a.shares, a.fill
    e["result"] = None
    save(a.journal, doc)
    print("#%d %s marked taken: %d shares at %.2f." % (e["id"], e["symbol"], a.shares, a.fill))
    return 0


def cmd_skip(a):
    doc = load(a.journal)
    e = find(doc, a.id)
    e["status"] = "skipped"
    if a.note:
        e["note"] = a.note
    save(a.journal, doc)
    print("#%d %s marked skipped. It is still scored, so you can see what passing on it cost or saved." % (e["id"], e["symbol"]))
    return 0


def cmd_close(a):
    doc = load(a.journal)
    e = find(doc, a.id)
    if e.get("fill") is None:
        raise JournalError("#%d has no fill: run `journal.py take %d --shares N --fill PRICE` first" % (a.id, a.id))
    e["exit"], e["exit_date"] = a.exit, a.date or today()
    risk = e["fill"] - e["stop"]
    e["result"] = {"outcome": "closed by you", "entry": e["fill"], "exit": a.exit, "exit_date": e["exit_date"],
                   "r": round((a.exit - e["fill"]) / risk, 2) if risk > 0 else None, "source": "your fills"}
    save(a.journal, doc)
    print("#%d %s closed at %.2f: %+.2fR, %s." % (e["id"], e["symbol"], a.exit, e["result"]["r"] or 0,
                                                 "${:,.2f}".format(e["shares"] * (a.exit - e["fill"]))))
    return 0


def cmd_update(a):
    doc = load(a.journal)
    todo = [e for e in doc["entries"] if e["result"] is None]
    if not todo:
        print("Nothing open in the journal.")
        return 0
    n_done = 0
    for e in todo:
        try:
            bars = edge.cached_bars(e["symbol"], "1y", a.cache, max_age_hours=6)
        except edge.EdgeError as err:
            print("  #%d %s: %s" % (e["id"], e["symbol"], err))
            continue
        res = resolve(e, bars)
        if res:
            e["result"] = res
            n_done += 1
            print("  #%d %s %s: %s, %+.2fR" % (e["id"], e["date"], e["symbol"], res["outcome"], res["r"] or 0))
    save(a.journal, doc)
    print("Scored %d of %d open idea%s; the rest are still running." % (n_done, len(todo), "" if len(todo) == 1 else "s"))
    return 0


def cmd_list(a):
    doc = load(a.journal)
    rows = sorted(doc["entries"], key=lambda e: (e["date"], e["id"]), reverse=True)[:a.show]
    if not rows:
        print("The journal is empty. Log ideas with `journal.py add`.")
        return 0
    print("  %4s %-10s %-6s %-14s %-9s %8s %8s %8s  %-8s %s" % ("#", "date", "sym", "setup", "status", "entry",
                                                                "stop", "target", "market", "result"))
    for e in rows:
        r = e["result"]
        res = "open" if not r else "%s %+.2fR" % (r["outcome"], r["r"] or 0)
        print("  %4d %-10s %-6s %-14s %-9s %8.2f %8.2f %8.2f  %-8s %s" % (
            e["id"], e["date"], e["symbol"], (e["setup"] or "")[:14], e["status"], e["fill"] or e["planned_entry"],
            e["stop"], e["target"], (e["regime"] or "?")[:8], res))
    return 0


def cmd_stats(a):
    doc = load(a.journal)
    s = stats(doc["entries"])
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    print("JOURNAL: %d scored, %d still open" % (s["resolved"], s["open"]))

    def line(name, b):
        if b:
            print("  %-22s %4d   win %5.1f%%   avg %+.2fR   total %+6.1fR   PF %s" % (
                name, b["n"], b["win_pct"], b["avg_r"], b["total_r"], "-" if b["profit_factor"] is None else b["profit_factor"]))
    line("every idea", s["all"])
    line("ones you took", s["taken"])
    line("ones you didn't", s["not_taken"])
    for title, key in (("By setup", "by_setup"), ("By market regime", "by_regime")):
        if s[key]:
            print(title + ":")
            for k, b in s[key].items():
                line(k, b)
    if s["taken_pnl_usd"] is not None:
        print("  Money on the trades you took: %s" % "${:,.2f}".format(s["taken_pnl_usd"]))
    if s["resolved"] < MIN_TRADES:
        print("Only %d scored. Under %d, a result is mostly luck either way: keep logging." % (s["resolved"], MIN_TRADES))
    elif s["all"]["avg_r"] <= 0:
        print("Across %d ideas the average is %+.2fR: so far the hunt has no edge. Stop or change the rules before "
              "risking more." % (s["resolved"], s["all"]["avg_r"]))
    else:
        print("Across %d ideas the average is %+.2fR. Positive so far; keep logging, and compare it with the backtest."
              % (s["resolved"], s["all"]["avg_r"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Log every swing idea and score it honestly.")
    ap.add_argument("--journal", default="trader/journal.json")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--status", default="suggested", choices=["suggested", "taken", "skipped"])
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float)
    p.add_argument("--stop", type=float)
    p.add_argument("--target", type=float)
    p.add_argument("--hold", type=int, default=30)
    p.add_argument("--setup")
    p.add_argument("--date", help="the day it was found, YYYY-MM-DD (default: the plan's date or today)")
    p.add_argument("--regime", default="trader/regime.json")
    p.add_argument("--regime-state", choices=["risk-on", "caution", "risk-off"])
    p.add_argument("--rs", type=float)
    p.add_argument("--note")
    p = sub.add_parser("take")
    p.add_argument("id", type=int)
    p.add_argument("--shares", type=int, required=True)
    p.add_argument("--fill", type=float, required=True)
    p = sub.add_parser("skip")
    p.add_argument("id", type=int)
    p.add_argument("--note")
    p = sub.add_parser("close")
    p.add_argument("id", type=int)
    p.add_argument("--exit", type=float, required=True)
    p.add_argument("--date")
    p = sub.add_parser("update")
    p.add_argument("--cache", default="trader/cache/daily")
    p = sub.add_parser("list")
    p.add_argument("--show", type=int, default=30)
    p = sub.add_parser("stats")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"add": cmd_add, "take": cmd_take, "skip": cmd_skip, "close": cmd_close, "update": cmd_update,
                "list": cmd_list, "stats": cmd_stats}[a.cmd](a)
    except (JournalError, edge.EdgeError) as e:
        print("journal.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
