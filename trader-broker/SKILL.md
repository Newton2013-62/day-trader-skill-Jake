---
name: trader-broker
description: >-
  Order Executor. Sends approved orders to your broker, never without your
  yes. This fork is set up for Charles Schwab's Trader API (LIVE, real money:
  Schwab has no paper trading) through a local schwab-trader connector, with
  Alpaca (paper) and Interactive Brokers still supported. Shows each order as a plain ticket (a
  bracket shows its stop-loss, take-profit and the max loss at the stop)
  plus the exact JSON for Alpaca's place_stock_order, records your yes word
  for word, then Claude's own Allow once / Deny card asks again. Builds the
  sell ticket that closes a day trade by your flat-by time, records fills,
  and logs each trade's max loss so trader-risk can hold the daily limit.
  Covers signing in to Schwab and adding the schwab-trader connector, Alpaca's
  official MCP server (paper), the Interactive Brokers connector, and setting
  order tools to "Needs approval". Keys stay in env vars or local files, never
  in chat. Use when the user says "place the order", "buy it", "execute",
  "send it to my broker", "close my position", "flatten", "get me out",
  "connect Schwab", "connect Alpaca", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 8 --filled-price 364.20
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol TSLA --side sell --qty 8 --price 370.04
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol TSLA --qty 8
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

```bash
python3 "${CLAUDE_SKILL_DIR}/schwab.py" status                   # Schwab sign-in and account
python3 "${CLAUDE_SKILL_DIR}/schwab.py" account                  # cash, buying power, positions (read only)
python3 "${CLAUDE_SKILL_DIR}/schwab.py" preview --index 1        # the Schwab order JSON; sends nothing
python3 "${CLAUDE_SKILL_DIR}/schwab.py" place --index 1          # LIVE: only after risk PASS + recorded yes
python3 "${CLAUDE_SKILL_DIR}/schwab.py" orders                   # today's Schwab orders and fills
python3 "${CLAUDE_SKILL_DIR}/schwab.py" position TSLA            # shares held, for flatten --qty
python3 "${CLAUDE_SKILL_DIR}/schwab.py" cancel ORDER_ID          # cancel one open order
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the flatten ticket, and keeps the log in
`trader/state/broker.json`. `--now "2026-10-19 10:15"` pins the clock (New York time).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |

## Setup S: Charles Schwab (this fork's default, LIVE)

`trader/config.json` says `"broker": "schwab"` and `"paper": false`. **Schwab's
Trader API has no paper account: every order is real money.** The three gates
are what stand between a ticket and the market, so never weaken them.

1. The user has an app at developer.schwab.com (Trader API, Individual) in
   "Ready For Use" status, with a callback URL (usually `https://127.0.0.1`).
2. The user puts the app key and secret on their own computer, never in the
   chat: environment variables `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET` (and
   `SCHWAB_CALLBACK_URL` if it is not `https://127.0.0.1`), or a file
   `~/.schwab-trader/app.json` with `"app_key"`, `"app_secret"`, `"callback_url"`.
   If they paste a key or secret into the chat anyway, tell them to delete the
   message and reset the secret in the Schwab developer portal.
3. The user signs in **in their own terminal**: `python3 trader-broker/schwab.py auth`.
   It prints a Schwab link; they sign in, approve the accounts, and paste the
   address Schwab redirects to (it holds a one-time code that expires in about
   30 seconds). The sign-in is saved to `~/.schwab-trader/token.json`, readable
   by them only. Never run `auth` for them, never read that file into the chat.
   It lasts **7 days**; after that `schwab.py` says to run `auth` again.
4. More than one Schwab account: set `"schwab_account"` in `trader/config.json`
   to the account number or its last 4 digits (or `SCHWAB_ACCOUNT_NUMBER`).
5. Add the connector. Claude Code, from the project folder:
   `claude mcp add schwab -- python3 /path/to/trader-broker/schwab_mcp.py`
   (or the block in `schwab_mcp.example.json` in `.mcp.json`). Claude Desktop:
   the same block in `claude_desktop_config.json`, with `TRADER_DIR` set to the
   project folder. Restart.
6. **Permissions.** `schwab_place_order` and `schwab_cancel_order`: Needs
   approval (Desktop) or, in Claude Code, `.claude/settings.json`:
   `"permissions": {"ask": ["mcp__schwab__schwab_place_order", "mcp__schwab__schwab_cancel_order",
   "Bash(python3 trader-broker/schwab.py place:*)", "Bash(python3 trader-broker/schwab.py cancel:*)"]}`.
   Never put them in `allow`. The read-only tools can be allowed.
7. Check it: `schwab_get_account` (or `schwab.py status`) and confirm the
   account's last 4 digits with the user.

`schwab_place_order` (and `schwab.py place`) refuses unless trader-risk passed
that exact order today and `ticket.py approve` recorded the user's yes for it,
and it records placed or failed in `trader/state/broker.json` itself. A bracket
goes to Schwab as a TRIGGER: the entry, then an OCO pair (take-profit LIMIT and
stop-loss STOP) that Schwab sends once the entry fills. Whole shares only; no
dollar-amount, trailing or extended-hours orders.

## Setup A: Alpaca (official MCP server, Claude Desktop)

Alpaca's own server is github.com/alpacahq/alpaca-mcp-server (MIT). It runs on
the user's computer inside Claude Desktop. It is not in Claude's connector
directory. It trades on a **paper** account unless told otherwise. Checked
against its source on 2026-10-20 (version 2.4.0).

1. The user makes an Alpaca account and creates **paper trading** API keys in
   the Alpaca dashboard. Paper keys are separate from live keys.
2. The user installs `uv` (it provides `uvx`), from docs.astral.sh/uv.
3. In Claude Desktop: Settings > Developer > Edit Config. That opens
   `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
   Windows: `%APPDATA%\Claude\`). The user adds this block (also in
   `claude_desktop_config.example.json` here) and types their own keys into the file:

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "paste your paper key here, in this file only",
        "ALPACA_SECRET_KEY": "paste your paper secret here, in this file only",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,trading,stock-data"
      }
    }
  }
}
```

   The `env` block hands the keys to the server as environment variables.
   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes a key anyway, tell them
   to delete that message and make new keys in the Alpaca dashboard.
   `ALPACA_PAPER_TRADE` is already true by default; writing it makes it
   visible. `ALPACA_TOOLSETS` is optional and hides the tools they do not need.
4. Restart Claude Desktop.
5. **Set the tool permissions.** Customize > Connectors (Settings > Connectors
   in older versions), open alpaca, and for each tool pick Always allow, Needs
   approval or Blocked:
   - `place_stock_order`: **Needs approval**. Never Always allow.
   - every other tool that places, changes, cancels or closes anything
     (`cancel_order_by_id`, `replace_order_by_id`, `close_position`,
     `update_account_config`): Needs approval.
   - `place_option_order`, `place_crypto_order`, `close_all_positions`,
     `cancel_all_orders`, `exercise_options_position`,
     `do_not_exercise_options_position`: **Blocked** unless the user has a
     reason and says so.
   - read-only tools (`get_account_info`, `get_all_positions`, `get_open_position`,
     `get_orders`, `get_clock`, `get_stock_snapshot`, `get_stock_latest_quote`):
     Always allow is fine.
6. Check it: ask Claude for the Alpaca account info (read only) and confirm
   in the Alpaca dashboard that it is the paper account.

In Claude Code instead of Claude Desktop, keep the order tool on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__alpaca__place_stock_order"]}`,
and never put an order tool in `allow`.

**Bracket orders on Alpaca.** `place_stock_order` takes
`take_profit_limit_price` and `stop_loss_stop_price` (and an optional
`stop_loss_limit_price`). When either is there, the server sets
`order_class` to "bracket" by itself; trader-budget writes it anyway so the
ticket shows it. A bracket is whole shares only, with `time_in_force` day or
gtc. After the entry fills, the stop-loss and the take-profit wait at the
broker as two orders that cancel each other. They are day orders too, so they
end at the close: one more reason to be flat by then.

## Setup B: Interactive Brokers (connector in Claude's directory)

Interactive Brokers publishes a verified connector: claude.ai/directory/interactive-brokers
(connector URL `https://api.ibkr.com/v1/api/mcp`). Add it from Customize >
Connectors > Browse connectors and sign in to IBKR there, not in the chat.
Its listing says Claude can draft trade instructions that are sent to the
user's IBKR account for their review and approval, and that Claude never
submits orders directly to the market. So with IBKR the final yes happens in
IBKR as well. Set `"broker": "ibkr"` in `trader/config.json`. Set its tools
that draft orders to Needs approval too.

## Steps: the entry

With Schwab, steps 4 and 5 are one call: `schwab_place_order` with
`{"index": N}` (or `schwab.py place --index N`) sends the order and records
the result. Then `schwab_get_order` / `schwab.py order ID` shows the fill and
prints the `ticket.py fill` line to record it. Every other step is the same.

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes", call `place_stock_order` with exactly the
   JSON the ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id> --filled-qty Q --filled-price P` (the fill from the
   response or `get_open_position`), or `--status denied`, or `--status failed`
   with `--note`. A failed order is not retried without a new check and a new yes.
   The log keeps the entry's max loss for trader-risk's daily limit.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The bracket's own orders do the stopping.
- When the stop-loss or the take-profit fills, record it:
  `ticket.py fill --symbol TSLA --side sell --qty 8 --price 358.32`.
- By `flat_by` (15:55 New York), anything still open gets closed:
  1. Ask Alpaca how many shares are held: `get_open_position` (read only).
  2. `ticket.py flatten --symbol TSLA --qty 8` writes a market sell for those
     shares to `trader/orders.json`.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. The bracket's stop-loss and take-profit still hold those shares, so
     Alpaca rejects a new sell until they are canceled: `get_orders` (status
     open, nested true) shows them; `cancel_order_by_id` each one (Needs
     approval). Then `place_stock_order` with the ticket, then `result`.

## Example: the reel

```
ORDER 1 of 1, PAPER account, Alpaca
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  take-profit 370.04
  max loss about $46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  why: opening-range breakout on TSLA: 8 shares, bound by your $50 max loss per trade (8 x 5.86 = $46.88 at the stop)
  READY: trader-risk PASS, waiting for your yes
  Claude will call place_stock_order with exactly:
    {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
     "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
```

The user replies "yes". Claude records it, calls `place_stock_order`, and the
card reads "Claude wants to use Place stock order" with that JSON and two
buttons: Allow once, Deny. Allow once sends it to the paper account. The
`why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never switches `ALPACA_PAPER_TRADE` or `"paper"` to live. The user does that themselves.
  (Schwab is always live; this fork's config already says so, by the user's choice.)
- Never runs `schwab.py auth` for the user or reads `~/.schwab-trader/` into the chat.
- Never calls `close_all_positions`, `cancel_all_orders`, option or crypto order tools on its own.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- This fork trades LIVE on Charles Schwab (`"broker": "schwab"`, `"paper": false`), the user's own
  decision. Schwab has no paper account, so every ticket is real money; Alpaca paper remains available
  by setting `"broker": "alpaca"`, `"paper": true`.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
