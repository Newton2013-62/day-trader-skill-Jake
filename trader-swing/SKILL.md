---
name: trader-swing
description: >-
  Swing Trader. Plans and checks trades you hold for days to a few weeks
  (5 days to 3 weeks), not minutes. Builds the plan from daily bars: entry,
  a stop under the normal daily noise (2 x the 14-day ATR by default, or the
  10-day low, a percent, or your price), a target (2R by default) and a max
  holding period (15 trading days), and warns when an earnings report likely
  falls inside the hold. Runs the multi-day Time Machine: 10,000 simulated
  holds drawn from the stock's own daily bars, gaps included, plus the same
  stop and target from every past day. Writes trader/plan.json with "style":
  "swing", so trader-budget sizes a good-till-canceled bracket (the stop stays
  at the broker overnight) and trader-risk applies the swing limits (open
  positions, total open risk). Use when the user says "swing trade", "hold it
  for a couple of weeks", "multi-day trade", "plan a swing", "where's my stop
  for a swing", "will earnings hit my trade", "replay this over two weeks", or
  wants to hold overnight.
argument-hint: "[plan TICKER | replay | earnings TICKER]"
---

# trader-swing

The rest of the pack is built for day trades: in at 10 AM, out by 3:55 PM. A
swing trade holds for days to weeks, and that changes three things:

1. **The stop has to survive the night.** A day order's stop-loss expires at
   the close. A swing bracket is **good until canceled** (gtc), so the stop and
   the target stay at the broker every day until one fills or you cancel them.
2. **Overnight gaps.** The stock can open below your stop. The stop then fills
   at the open, worse than planned. The max loss is the loss *at the stop*;
   a gap can make it bigger. Earnings reports are the biggest gaps, so the plan
   checks whether one likely falls inside the hold.
3. **Daily bars, wider stops.** The stop sits under the normal daily swing
   (the ATR), not under a 15-minute range, so you buy fewer shares for the
   same max loss.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/swing.py" plan NVDA --write                 # entry = last close, 2 x ATR stop, 2R, 15 days
python3 "${CLAUDE_SKILL_DIR}/swing.py" plan NVDA --entry 181.50 --stop low10 --target 2R --hold 10 --write
python3 "${CLAUDE_SKILL_DIR}/swing.py" plan NVDA --earnings 2027-02-25 --write   # you know the date
python3 "${CLAUDE_SKILL_DIR}/swing.py" replay                            # multi-day Time Machine on trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/swing.py" earnings NVDA                     # the estimated next report
```

Stops: `atr` (default, `--atr-mult 2`), `low10` (a cent under the 10-day low;
`low20` and so on work too), a percent like `6%`, or a price. Targets: `2R`,
`1.5R` or a price. Defaults come from `trader/config.json`: `swing_hold_days`,
`swing_atr_mult`, `swing_target_r`.

## Steps: one swing trade

1. Find a candidate. `/trader-scanner daily` lists the daily setups that suit a
   swing: a 20-day closing-high breakout, a bull flag, a reclaimed 20-day
   average, an oversold bounce. `/trader-news` and `/trader-analyst` for why it
   is moving.
2. `swing.py plan TICKER --write`. Read the warnings out loud, especially an
   **earnings** warning: if a report likely falls inside the hold, say plainly
   that the stock can gap past the stop, and offer to shorten the hold, skip
   it, or size it smaller.
3. `swing.py replay`. Report the three odds and the average R from both the
   simulation and the history, and the verdict word for word. Never round a
   coin flip up into a good trade.
4. `/trader-budget` sizes it (the swing plan makes it a gtc bracket), then
   `/trader-risk`, then `/trader-broker`: the user's yes for that one order,
   then the Allow once card. Same three gates as every order in the pack.
5. `/trader-alerts` with `alerts.py add --from-plan`: the stop and target are
   watched every day, and a time alert rings at 3:45 PM on the exit-by date.
6. On the exit-by date, if neither leg filled, close it the same way as a day
   trade's flatten: cancel the bracket's open legs, then a sell ticket
   (`ticket.py flatten --symbol T --qty N`), risk check, the user's yes.

## Does any of this work? Regime, relative strength, backtest, journal

A plan and a replay say what one trade might do. They cannot say whether the
whole approach makes money. Four tools in this folder answer that:

```bash
python3 "${CLAUDE_SKILL_DIR}/edge.py" regime                 # is the market in an uptrend?
python3 "${CLAUDE_SKILL_DIR}/edge.py" rs --write             # rank the watchlist by strength vs SPY
python3 "${CLAUDE_SKILL_DIR}/backtest.py" --sample 500       # the hunt's rules over 5 years
python3 "${CLAUDE_SKILL_DIR}/journal.py" add                 # log trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/journal.py" update              # score open ideas from daily bars
python3 "${CLAUDE_SKILL_DIR}/journal.py" stats
```

- **Regime** (`edge.py regime`, writes `trader/regime.json`). SPY against its
  50- and 200-day averages: **risk-on** (above both, 50-day rising),
  **caution** (above the 200-day only), **risk-off** (under the 200-day).
  QQQ and IWM are shown beside it. In risk-off the default is no new swing
  entries; say so plainly, and let the user decide.
- **Relative strength** (`edge.py rs`). Each stock's return minus SPY's over
  1, 3 and 6 months (weights 20/50/30), and its rank 1-99 in the list. With
  `--write` it stores `rs` and `rs_rank` in `trader/watchlist.json`. Rank the
  hunt's candidates by it: stocks already beating the market break out more
  cleanly than laggards.
- **Backtest** (`backtest.py`, writes `trader/backtest.json`). Every scanner
  setup over the last N years (default 5) on a seeded sample of the universe
  (`--all` for every stock), entry at the next open, the config's ATR stop,
  R target and hold, 0.1 % slippage each way. Four variants side by side:
  every signal, risk-on only, beating SPY only, and both. Each is also run as
  the user's real account (budget, positions, $ risk, max order, open-risk
  cap), marked to market daily, against just holding SPY. Report the table,
  the by-year lines and the verdict as printed. Always say the survivorship
  caveat: today's list has no delisted losers, so results look too good.
- **Journal** (`journal.py`, writes `trader/journal.json`). Log EVERY idea the
  hunt ranks, taken or not, before anyone knows how it ends. `update` scores
  them from daily bars (entry at the next open, then stop, target or time);
  `take` / `close` record the user's real fills, which win over the bars.
  `stats` gives win rate, average R and profit factor, split by taken vs not,
  setup and regime. Under 30 scored ideas, say the result is mostly luck.

The order of trust: the journal (real, forward) over the backtest (real
history, flattered by survivorship) over the replay (a resampled guess).

## The swing rules in trader-risk (trader/config.json)

| key | default | what |
| --- | --- | --- |
| `max_loss_per_trade_usd` | 50 | the same per-trade cap as day trades |
| `max_open_positions` | 3 | stocks held at once |
| `max_open_risk_usd` | 150 | the max losses at the stops of every open swing trade, added up |
| `swing_hold_days` | 15 | the time limit, in trading days |

A swing bracket that is not gtc is blocked: its stop would vanish at the close.

## What the Time Machine can and cannot tell you

It replays the next N trading days 10,000 times, each day drawn from one of the
stock's last ~250 daily moves relative to the previous close (so overnight
gaps are in it), and it re-runs the same stop and target distances from every
past day of the last two years. If the stop and the target are both inside one
day's range, it counts the stop (a daily bar cannot show which came first).
It does not know about the next earnings report, news, or a market crash that
the last year never had. Treat its odds as a reality check, never a promise.

## Safety

- Education and tooling, not financial advice. A plan is arithmetic on past
  prices, not a prediction, and most people who trade actively underperform a
  plain index fund.
- No trade goes through until you say yes: only trader-broker can lead to an
  order, after trader-risk passes that exact order and you type yes for it in
  the chat, then click Allow once on Claude's own card. Never set an order tool
  to "Always allow".
- Long only. No options, margin or short selling unless the config allows it.
- A stop is a plan, not a guarantee: overnight it can fill well below its price.
