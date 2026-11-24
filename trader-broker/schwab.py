#!/usr/bin/env python3
"""Charles Schwab Trader API for trader-broker. Standard library only.

Schwab has no paper trading: every order this tool places is a LIVE order
with real money. So it only places an order that already went through the
pack's gates: trader-risk passed THIS exact order today (matched by
fingerprint) and ticket.py recorded the user's explicit yes for it. Claude's
own permission card (Allow once / Deny) is the third gate: keep this tool's
"place" command and the schwab_place_order connector tool on ask.

    python3 schwab.py auth                  # sign in to Schwab (run it in YOUR terminal)
    python3 schwab.py status                # which account, when the sign-in expires
    python3 schwab.py account               # cash, buying power and positions (read only)
    python3 schwab.py position TSLA         # shares held, for ticket.py flatten --qty
    python3 schwab.py orders                # today's orders (read only)
    python3 schwab.py order ORDER_ID        # one order: status and fills
    python3 schwab.py preview --index 1     # the exact Schwab order JSON; sends nothing
    python3 schwab.py place --index 1       # LIVE: places order 1 after your recorded yes
    python3 schwab.py cancel ORDER_ID       # cancels one open order

Credentials never go in the chat and never in trader/config.json:

  SCHWAB_APP_KEY, SCHWAB_APP_SECRET   your app's key and secret from developer.schwab.com
                                      (or a JSON file at ~/.schwab-trader/app.json with
                                      "app_key", "app_secret" and optionally "callback_url")
  SCHWAB_CALLBACK_URL                 the app's callback URL (default https://127.0.0.1)
  SCHWAB_TOKEN_FILE                   where the sign-in is kept (default ~/.schwab-trader/token.json,
                                      readable by you only)
  SCHWAB_ACCOUNT_NUMBER               which account, if you have more than one (or
                                      "schwab_account" in trader/config.json: the number or its last 4)

Schwab's access token lasts 30 minutes and is refreshed automatically. The
refresh token lasts 7 days; after that, run `schwab.py auth` again.
"""

import argparse
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402  (same folder)

API = "https://api.schwabapi.com"
AUTH_URL = API + "/v1/oauth/authorize"
TOKEN_URL = API + "/v1/oauth/token"
TRADER = API + "/trader/v1"
DEFAULT_CALLBACK = "https://127.0.0.1"
REFRESH_DAYS = 7
HOME_DIR = os.path.join(os.path.expanduser("~"), ".schwab-trader")

TIF = {"day": "DAY", "gtc": "GOOD_TILL_CANCEL", "fok": "FILL_OR_KILL", "ioc": "IMMEDIATE_OR_CANCEL"}
SIDE = {"buy": "BUY", "sell": "SELL"}
EXIT_SIDE = {"buy": "SELL", "sell": "BUY"}


class SchwabError(Exception):
    pass


# ---------------------------------------------------------------- order conversion (no network)

def _price(v):
    f = float(v)
    if f <= 0:
        raise SchwabError("a price must be positive, got %r" % v)
    return ("%.2f" % f) if f >= 1 else ("%.4f" % f)


def _qty(order):
    if order.get("notional") is not None:
        raise SchwabError("Schwab orders need a number of shares, not a dollar amount (notional)")
    q = ticket._f(order.get("qty"))
    if q is None or q <= 0:
        raise SchwabError("the order has no positive qty")
    if q != int(q):
        raise SchwabError("Schwab's API takes whole shares only; this order is for %s" % order.get("qty"))
    return int(q)


def _leg(instruction, qty, symbol):
    return {"instruction": instruction, "quantity": qty,
            "instrument": {"symbol": symbol, "assetType": "EQUITY"}}


def _single(order_type, duration, leg, price=None, stop=None):
    o = {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": duration, "orderType": order_type}
    if price is not None:
        o["price"] = _price(price)
    if stop is not None:
        o["stopPrice"] = _price(stop)
    o["orderLegCollection"] = [leg]
    return o


def to_schwab(order):
    """The pack's order (Alpaca's field names, as trader-budget writes it) -> Schwab order JSON.

    A bracket becomes a TRIGGER: the entry first; once it fills, the take-profit
    (LIMIT) and the stop-loss (STOP, or STOP_LIMIT) go in as an OCO pair, so
    when one fills Schwab cancels the other. "why" and "plan" are never sent."""
    symbol = str(order.get("symbol", "")).strip().upper()
    if not symbol:
        raise SchwabError("the order has no symbol")
    side = str(order.get("side", "")).lower()
    if side not in SIDE:
        raise SchwabError("side must be buy or sell, got %r" % order.get("side"))
    tif = str(order.get("time_in_force", "day")).lower()
    if tif not in TIF:
        raise SchwabError("time_in_force %r is not supported for Schwab (use day or gtc)" % tif)
    if order.get("extended_hours"):
        raise SchwabError("extended-hours orders are not supported by this adapter")
    if order.get("advanced_instructions") or order.get("trail_price") or order.get("trail_percent"):
        raise SchwabError("trailing and advanced orders are not supported by this adapter")
    qty = _qty(order)
    duration = TIF[tif]
    typ = str(order.get("type", "market")).lower()
    leg = _leg(SIDE[side], qty, symbol)
    if typ == "market":
        # Schwab takes market orders for the day only; a gtc bracket keeps its legs gtc (below)
        parent = _single("MARKET", "DAY", leg)
    elif typ == "limit":
        parent = _single("LIMIT", duration, leg, price=order.get("limit_price"))
    elif typ == "stop":
        parent = _single("STOP", duration, leg, stop=order.get("stop_price"))
    elif typ == "stop_limit":
        parent = _single("STOP_LIMIT", duration, leg, price=order.get("limit_price"), stop=order.get("stop_price"))
    else:
        raise SchwabError("order type %r is not supported for Schwab" % typ)

    if not ticket.is_bracket(order):
        return parent
    if tif not in ("day", "gtc"):
        raise SchwabError("a bracket must be good for the day or good until canceled")
    exit_side = EXIT_SIDE[side]
    children = []
    tp = order.get("take_profit_limit_price")
    if tp is not None:
        children.append(_single("LIMIT", duration, _leg(exit_side, qty, symbol), price=tp))
    sl = order.get("stop_loss_stop_price")
    if sl is not None:
        if order.get("stop_loss_limit_price") is not None:
            children.append(_single("STOP_LIMIT", duration, _leg(exit_side, qty, symbol),
                                    price=order["stop_loss_limit_price"], stop=sl))
        else:
            children.append(_single("STOP", duration, _leg(exit_side, qty, symbol), stop=sl))
    if not children:
        raise SchwabError("a bracket needs a stop-loss or a take-profit")
    parent["orderStrategyType"] = "TRIGGER"
    if len(children) == 1:
        parent["childOrderStrategies"] = children
    else:
        parent["childOrderStrategies"] = [{"orderStrategyType": "OCO", "childOrderStrategies": children}]
    return parent


# ---------------------------------------------------------------- credentials and tokens

def app_credentials():
    key = os.environ.get("SCHWAB_APP_KEY", "").strip()
    secret = os.environ.get("SCHWAB_APP_SECRET", "").strip()
    callback = os.environ.get("SCHWAB_CALLBACK_URL", "").strip()
    path = os.path.join(HOME_DIR, "app.json")
    if (not key or not secret) and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        key = key or str(doc.get("app_key", "")).strip()
        secret = secret or str(doc.get("app_secret", "")).strip()
        callback = callback or str(doc.get("callback_url", "")).strip()
    if not key or not secret:
        raise SchwabError("no Schwab app key/secret: set SCHWAB_APP_KEY and SCHWAB_APP_SECRET, or put them "
                          "in %s (never in the chat)" % path)
    return key, secret, callback or DEFAULT_CALLBACK


def token_path():
    return os.environ.get("SCHWAB_TOKEN_FILE") or os.path.join(HOME_DIR, "token.json")


def save_token(tok, path=None):
    path = path or token_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(tok, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_token(path=None):
    path = path or token_path()
    if not os.path.exists(path):
        raise SchwabError("not signed in to Schwab yet: run `python3 schwab.py auth` in your own terminal")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- HTTP

class Client:
    """Talks to Schwab. `opener` is swapped out in the tests; nothing else changes."""

    def __init__(self, opener=None, clock=time.time, tokens=None):
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self.tokens = tokens

    def _http(self, method, url, headers=None, body=None):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with self.opener(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""
        except urllib.error.URLError as e:
            raise SchwabError("could not reach Schwab: %s" % e.reason)

    # -- tokens
    def _token_request(self, form):
        key, secret, _ = app_credentials()
        basic = base64.b64encode(("%s:%s" % (key, secret)).encode()).decode()
        st, _, body = self._http("POST", TOKEN_URL, {
            "Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode(form).encode())
        if st != 200:
            raise SchwabError("Schwab refused the sign-in (HTTP %d): %s" % (st, _short(body)))
        doc = json.loads(body.decode() or "{}")
        now = self.clock()
        tok = {"access_token": doc["access_token"], "refresh_token": doc.get("refresh_token"),
               "expires_at": now + int(doc.get("expires_in", 1800)) - 60}
        return tok

    def exchange_code(self, code, callback):
        tok = self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": callback})
        tok["refresh_expires_at"] = self.clock() + REFRESH_DAYS * 86400
        save_token(tok)
        self.tokens = tok
        return tok

    def access_token(self):
        tok = self.tokens or load_token()
        if self.clock() < tok.get("expires_at", 0):
            self.tokens = tok
            return tok["access_token"]
        if not tok.get("refresh_token") or self.clock() >= tok.get("refresh_expires_at", 0):
            raise SchwabError("the Schwab sign-in expired (it lasts 7 days): run `python3 schwab.py auth` again")
        new = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new["refresh_token"] = new.get("refresh_token") or tok["refresh_token"]
        new["refresh_expires_at"] = tok["refresh_expires_at"]
        save_token(new)
        self.tokens = new
        return new["access_token"]

    # -- API
    def call(self, method, path, body=None, ok=(200,)):
        headers = {"Authorization": "Bearer " + self.access_token(), "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        st, hdrs, raw = self._http(method, TRADER + path, headers, data)
        if st not in ok:
            raise SchwabError("Schwab said HTTP %d for %s %s: %s" % (st, method, path, _short(raw)))
        return st, hdrs, (json.loads(raw.decode()) if raw.strip() else None)

    def accounts(self):
        return self.call("GET", "/accounts/accountNumbers")[2] or []

    def account_hash(self, wanted=None):
        accts = self.accounts()
        if not accts:
            raise SchwabError("Schwab returned no accounts for this sign-in")
        wanted = str(wanted or "").strip()
        if wanted:
            hits = [a for a in accts if str(a.get("accountNumber", "")).endswith(wanted)]
            if len(hits) != 1:
                raise SchwabError("no single Schwab account matches %s (you have %s)"
                                  % (mask(wanted), ", ".join(mask(a.get("accountNumber")) for a in accts)))
            return hits[0]["hashValue"], hits[0]["accountNumber"]
        if len(accts) > 1:
            raise SchwabError("you have %d Schwab accounts (%s): say which with SCHWAB_ACCOUNT_NUMBER or "
                              "\"schwab_account\" in trader/config.json"
                              % (len(accts), ", ".join(mask(a.get("accountNumber")) for a in accts)))
        return accts[0]["hashValue"], accts[0]["accountNumber"]

    def account(self, h):
        return self.call("GET", "/accounts/%s?fields=positions" % h)[2] or {}

    def orders(self, h, now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        frm = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to = (now + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        q = urllib.parse.urlencode({"fromEnteredTime": frm, "toEnteredTime": to})
        return self.call("GET", "/accounts/%s/orders?%s" % (h, q))[2] or []

    def order(self, h, order_id):
        return self.call("GET", "/accounts/%s/orders/%s" % (h, urllib.parse.quote(str(order_id))))[2] or {}

    def place(self, h, schwab_order):
        st, hdrs, _ = self.call("POST", "/accounts/%s/orders" % h, schwab_order, ok=(200, 201))
        loc = hdrs.get("Location") or hdrs.get("location") or ""
        return loc.rstrip("/").rsplit("/", 1)[-1] if loc else None

    def cancel(self, h, order_id):
        self.call("DELETE", "/accounts/%s/orders/%s" % (h, urllib.parse.quote(str(order_id))), ok=(200, 204))


def _short(raw):
    t = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return t.strip().replace("\n", " ")[:300] or "(no details)"


def mask(number):
    s = str(number or "")
    return "..." + s[-4:] if len(s) > 4 else s


def wanted_account(cfg):
    return os.environ.get("SCHWAB_ACCOUNT_NUMBER") or cfg.get("schwab_account")


# ---------------------------------------------------------------- read-only summaries

def summarize_account(doc, number):
    sa = doc.get("securitiesAccount", doc)
    bal = sa.get("currentBalances", {})
    lines = ["Schwab account %s (%s), LIVE" % (mask(number), sa.get("type", "?"))]
    for k, label in (("liquidationValue", "account value"), ("cashBalance", "cash"),
                     ("cashAvailableForTrading", "cash available to trade"), ("buyingPower", "buying power"),
                     ("dayTradingBuyingPower", "day-trading buying power")):
        if bal.get(k) is not None:
            lines.append("  %s: %s" % (label, ticket._money(bal[k])))
    pos = sa.get("positions") or []
    lines.append("  positions: %d" % len(pos))
    for p in pos:
        ins = p.get("instrument", {})
        q = float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
        lines.append("    %s %s shares, avg %s, value %s" % (
            ins.get("symbol"), ticket._str_num(q), ticket._p(p.get("averagePrice", 0)),
            ticket._money(p.get("marketValue", 0))))
    return "\n".join(lines)


def position_qty(doc, symbol):
    sa = doc.get("securitiesAccount", doc)
    for p in sa.get("positions") or []:
        if str(p.get("instrument", {}).get("symbol", "")).upper() == symbol.upper():
            return float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
    return 0.0


def fill_of(order_doc):
    """(filled qty, average price or None) from Schwab's order JSON."""
    q, cost = 0.0, 0.0
    for act in order_doc.get("orderActivityCollection") or []:
        for leg in act.get("executionLegs") or []:
            lq, lp = float(leg.get("quantity", 0)), float(leg.get("price", 0))
            q += lq
            cost += lq * lp
    if q <= 0:
        q = float(order_doc.get("filledQuantity", 0) or 0)
        return q, None
    return q, round(cost / q, 4)


def summarize_order(o):
    legs = o.get("orderLegCollection") or [{}]
    leg = legs[0]
    q, avg = fill_of(o)
    price = o.get("price") or o.get("stopPrice") or ""
    line = "%s  %s %s %s %s %s %s  filled %s%s" % (
        o.get("orderId"), o.get("status"), leg.get("instruction"), ticket._str_num(leg.get("quantity", 0)),
        leg.get("instrument", {}).get("symbol"), o.get("orderType"), price, ticket._str_num(q),
        (" at %s" % ticket._p(avg)) if avg else "")
    out = [line]
    for child in o.get("childOrderStrategies") or []:
        for sub in summarize_order(child).splitlines():
            out.append("  " + sub)
    return "\n".join(out)


# ---------------------------------------------------------------- the gate

def approved_order(orders_path, config_path, risk_path, state_path, index, now_text=None):
    """The order at `index`, but only if trader-risk passed it today AND the user's yes
    for it is recorded today and nothing has happened since. Returns (order, record, cfg, today, now)."""
    doc = ticket.read_json(orders_path, "orders file")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise SchwabError("no orders in %s" % orders_path)
    order = ticket._pick(orders, index)
    cfg = ticket.read_json(config_path, "config", required=False) or {}
    if str(cfg.get("broker", "")).lower() != "schwab":
        raise SchwabError("trader/config.json does not say \"broker\": \"schwab\"; refusing to send to Schwab")
    now = ticket.parse_now(now_text)
    today = ticket.to_eastern(now).date().isoformat()
    fp = ticket.fingerprint(order)
    risk = ticket.read_json(risk_path, "risk check", required=False)
    rv, why = ticket.risk_verdict(risk, fp, today)
    if rv is None:
        raise SchwabError("not placed: " + why)
    if rv.get("verdict") != "PASS":
        raise SchwabError("not placed: trader-risk BLOCKED this order: " + "; ".join(rv.get("reasons", [])))
    state = ticket.load_state(state_path)
    rec = ticket.latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        if rec:
            raise SchwabError("not placed: order %d is already %s (at %s); a new order needs a new check "
                              "and a new yes" % (index, rec.get("status"), rec.get("at")))
        raise SchwabError("not placed: no yes recorded today for order %d. Show the ticket, get the user's "
                          "yes, run ticket.py approve, then place it" % index)
    return order, rec, cfg, today, now


def record(state_path, rec, index, today, now, status, broker_id=None, note=None):
    state = ticket.load_state(state_path)
    new = {"at": ticket.ny_iso(now), "date": today, "index": index, "fingerprint": rec["fingerprint"],
           "symbol": rec["symbol"], "side": rec["side"], "plain": rec["plain"], "status": status,
           "broker": "schwab"}
    for k in ("qty", "kind", "max_loss", "stop", "style"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if broker_id:
        new["broker_order_id"] = broker_id
    if note:
        new["note"] = note
    state.setdefault("orders", []).append(new)
    ticket.write_json(state_path, state)


def place_approved(client, orders_path, config_path, risk_path, state_path, index, now_text=None):
    """Places one approved order on Schwab and records the outcome. Returns a message."""
    order, rec, cfg, today, now = approved_order(orders_path, config_path, risk_path, state_path, index, now_text)
    body = to_schwab(order)
    try:
        h, number = client.account_hash(wanted_account(cfg))
        order_id = client.place(h, body)
    except SchwabError as e:
        record(state_path, rec, index, today, now, "failed", note=str(e))
        raise SchwabError("Schwab did not take order %d: %s. Recorded as failed; it is not retried without a "
                          "new check and a new yes." % (index, e))
    record(state_path, rec, index, today, now, "placed", broker_id=order_id)
    msg = ["PLACED on Schwab account %s (LIVE): %s" % (mask(number), rec["plain"]),
           "Schwab order id: %s" % (order_id or "(not returned; check `schwab.py orders`)"),
           "Recorded in %s. When it fills: schwab.py order %s, then ticket.py fill --symbol %s --side %s "
           "--qty Q --price P" % (state_path, order_id or "ID", rec["symbol"], rec["side"])]
    return "\n".join(msg)


# ---------------------------------------------------------------- CLI

def cmd_auth(a, client):
    key, _, callback = app_credentials()
    url = "%s?%s" % (AUTH_URL, urllib.parse.urlencode({"client_id": key, "redirect_uri": callback}))
    redirect = a.redirect_url
    if not redirect:
        print("1. Open this link, sign in to Schwab and approve the accounts:\n\n   %s\n" % url)
        print("2. Schwab sends you to %s?code=... (the page may not load; that is fine)." % callback)
        print("3. Copy the whole address from the browser bar and paste it here within 30 seconds.")
        print("   Do this in your own terminal. Never paste it into a chat.\n")
        redirect = input("Address: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)
    code = (q.get("code") or [""])[0]
    if not code:
        raise SchwabError("that address has no ?code= in it")
    client.exchange_code(code, callback)
    print("Signed in. Saved to %s (readable by you only). It lasts %d days." % (token_path(), REFRESH_DAYS))
    return 0


def cmd_status(a, client):
    tok = load_token()
    left = (tok.get("refresh_expires_at", 0) - time.time()) / 86400.0
    print("Sign-in file: %s" % token_path())
    print("Sign-in expires in %.1f days%s." % (max(left, 0), "" if left > 0 else ": run schwab.py auth"))
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    for acct in client.accounts():
        print("Account %s" % mask(acct.get("accountNumber")))
    want = wanted_account(cfg)
    if want:
        print("Orders go to the account ending %s." % str(want)[-4:])
    print("Schwab has no paper trading: every order is LIVE.")
    return 0


def _hash(a, client):
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    return client.account_hash(wanted_account(cfg))


def cmd_account(a, client):
    h, number = _hash(a, client)
    doc = client.account(h)
    print(json.dumps(doc, indent=2) if a.json else summarize_account(doc, number))
    return 0


def cmd_position(a, client):
    h, _ = _hash(a, client)
    q = position_qty(client.account(h), a.symbol)
    print("%s %s shares held" % (a.symbol.upper(), ticket._str_num(q)))
    return 0 if q else 1


def cmd_orders(a, client):
    h, _ = _hash(a, client)
    rows = client.orders(h)
    if a.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No Schwab orders in the last day.")
    else:
        print("\n".join(summarize_order(o) for o in rows))
    return 0


def cmd_order(a, client):
    h, _ = _hash(a, client)
    o = client.order(h, a.order_id)
    print(json.dumps(o, indent=2) if a.json else summarize_order(o))
    q, avg = fill_of(o)
    if q and avg and not a.json:
        leg = (o.get("orderLegCollection") or [{}])[0]
        side = "buy" if str(leg.get("instruction", "")).upper().startswith("BUY") else "sell"
        print("Record it: ticket.py fill --symbol %s --side %s --qty %s --price %s" % (
            leg.get("instrument", {}).get("symbol"), side, ticket._str_num(q), ticket._p(avg)))
    return 0


def cmd_preview(a, client):
    doc = ticket.read_json(a.orders, "orders file")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    order = ticket._pick(orders, a.index)
    body = to_schwab(order)
    print("Order %d: %s" % (a.index, ticket.plain(order)))
    print("Schwab order JSON (nothing was sent):")
    print(json.dumps(body, indent=2))
    return 0


def cmd_place(a, client):
    print(place_approved(client, a.orders, a.config, a.risk, a.state, a.index, a.now))
    return 0


def cmd_cancel(a, client):
    h, _ = _hash(a, client)
    client.cancel(h, a.order_id)
    print("Asked Schwab to cancel order %s. Check it with: schwab.py order %s" % (a.order_id, a.order_id))
    return 0


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description="Charles Schwab Trader API for trader-broker (LIVE orders).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default="trader/config.json")

    def files(p):
        common(p)
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--now", help=argparse.SUPPRESS)

    p = sub.add_parser("auth", help="sign in to Schwab (in your own terminal)")
    p.add_argument("--redirect-url", help="the address Schwab sent you to, if you already have it")
    p = sub.add_parser("status", help="sign-in and account")
    common(p)
    for name in ("account", "orders"):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("position", help="shares held in one symbol")
    common(p)
    p.add_argument("symbol")
    p = sub.add_parser("order", help="one order's status and fills")
    common(p)
    p.add_argument("order_id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("preview", help="the Schwab JSON for one ticket; sends nothing")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("place", help="LIVE: place one approved ticket on Schwab")
    files(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("cancel", help="cancel one open order")
    common(p)
    p.add_argument("order_id")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    client = client or Client()
    try:
        return {"auth": cmd_auth, "status": cmd_status, "account": cmd_account, "position": cmd_position,
                "orders": cmd_orders, "order": cmd_order, "preview": cmd_preview, "place": cmd_place,
                "cancel": cmd_cancel}[a.cmd](a, client)
    except (SchwabError, ticket.TicketError) as e:
        print("schwab.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
