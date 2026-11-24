import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

import sys
sys.path.insert(0, os.path.join(ROOT, "trader-broker"))
import schwab  # noqa: E402
import schwab_mcp  # noqa: E402
import ticket  # noqa: E402  (the copy schwab.py uses)

check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = "2026-10-19 10:15"
HASH = "HASHVALUE123"


def tsla():
    with open(ORDERS_PATH) as fh:
        return json.load(fh)["orders"][0]


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSchwab:
    """Answers like Schwab's API and remembers every request."""

    def __init__(self, accounts=None, place_status=201):
        self.calls = []
        self.accounts = accounts if accounts is not None else [{"accountNumber": "12345678", "hashValue": HASH}]
        self.place_status = place_status

    def __call__(self, req, timeout=None):
        body = req.data.decode() if req.data else None
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        url, m = req.full_url, req.get_method()
        if url.endswith("/v1/oauth/token"):
            return Resp(200, json.dumps({"access_token": "AT-new", "refresh_token": "RT-new",
                                         "expires_in": 1800}).encode())
        if url.endswith("/accounts/accountNumbers"):
            return Resp(200, json.dumps(self.accounts).encode())
        if m == "POST" and url.endswith("/accounts/%s/orders" % HASH):
            if self.place_status != 201:
                raise urllib.error.HTTPError(url, self.place_status, "Bad Request", {},
                                             io.BytesIO(b'{"message":"insufficient buying power"}'))
            return Resp(201, b"", {"Location": schwab.TRADER + "/accounts/%s/orders/1000123" % HASH})
        if "fields=positions" in url:
            return Resp(200, json.dumps({"securitiesAccount": {"type": "MARGIN", "currentBalances": {
                "cashBalance": 5000.0, "liquidationValue": 5000.0}, "positions": [
                {"instrument": {"symbol": "TSLA"}, "longQuantity": 8.0, "shortQuantity": 0.0,
                 "averagePrice": 364.2, "marketValue": 2960.32}]}}).encode())
        if m == "DELETE":
            return Resp(200)
        if "/orders/1000123" in url:
            return Resp(200, json.dumps({"orderId": 1000123, "status": "FILLED", "orderType": "MARKET",
                                         "orderLegCollection": [{"instruction": "BUY", "quantity": 8,
                                                                 "instrument": {"symbol": "TSLA"}}],
                                         "orderActivityCollection": [{"executionLegs": [
                                             {"quantity": 5, "price": 364.2}, {"quantity": 3, "price": 364.3}]}]
                                         }).encode())
        raise AssertionError("unexpected request %s %s" % (m, url))

    def posted_orders(self):
        return [json.loads(c[3]) for c in self.calls if c[0] == "POST" and c[1].endswith("/orders")]


class Env(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update({"SCHWAB_APP_KEY": "key", "SCHWAB_APP_SECRET": "secret",
                           "SCHWAB_TOKEN_FILE": os.path.join(self.d, "tok", "token.json")})
        os.environ.pop("SCHWAB_ACCOUNT_NUMBER", None)
        self.now = 1_800_000_000.0

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.d)

    def client(self, fake, fresh=True):
        tok = {"access_token": "AT", "refresh_token": "RT", "expires_at": self.now + 600,
               "refresh_expires_at": self.now + 86400}
        if not fresh:
            tok["expires_at"] = self.now - 1
        schwab.save_token(tok)
        return schwab.Client(opener=fake, clock=lambda: self.now)


class Convert(unittest.TestCase):
    def test_the_reel_bracket_is_a_trigger_with_an_oco_pair(self):
        o = schwab.to_schwab(tsla())
        self.assertEqual(o["orderStrategyType"], "TRIGGER")
        self.assertEqual((o["orderType"], o["duration"], o["session"]), ("MARKET", "DAY", "NORMAL"))
        self.assertEqual(o["orderLegCollection"],
                         [{"instruction": "BUY", "quantity": 8, "instrument": {"symbol": "TSLA", "assetType": "EQUITY"}}])
        oco = o["childOrderStrategies"][0]
        self.assertEqual(oco["orderStrategyType"], "OCO")
        tp, sl = oco["childOrderStrategies"]
        self.assertEqual((tp["orderType"], tp["price"]), ("LIMIT", "370.04"))
        self.assertEqual((sl["orderType"], sl["stopPrice"]), ("STOP", "358.32"))
        for leg in (tp, sl):
            self.assertEqual(leg["orderLegCollection"][0]["instruction"], "SELL")
            self.assertEqual(leg["orderLegCollection"][0]["quantity"], 8)
        self.assertNotIn("plan", json.dumps(o))
        self.assertNotIn("why", json.dumps(o))

    def test_limit_entry_and_stop_limit_leg(self):
        o = schwab.to_schwab(dict(tsla(), type="limit", limit_price="364.18", stop_loss_limit_price="357.50",
                                  time_in_force="gtc"))
        self.assertEqual((o["orderType"], o["price"], o["duration"]), ("LIMIT", "364.18", "GOOD_TILL_CANCEL"))
        sl = o["childOrderStrategies"][0]["childOrderStrategies"][1]
        self.assertEqual((sl["orderType"], sl["stopPrice"], sl["price"]), ("STOP_LIMIT", "358.32", "357.50"))

    def test_a_plain_market_sell(self):
        o = schwab.to_schwab({"symbol": "tsla", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"})
        self.assertEqual(o["orderStrategyType"], "SINGLE")
        self.assertEqual(o["orderLegCollection"][0]["instruction"], "SELL")
        self.assertNotIn("childOrderStrategies", o)

    def test_refuses_what_schwab_cannot_take(self):
        for bad in ({"notional": "1250", "qty": None}, {"qty": "2.5"}, {"time_in_force": "opg"},
                    {"type": "trailing_stop"}, {"side": "sell_short"}, {"extended_hours": True}):
            o = dict(tsla())
            o.update(bad)
            with self.assertRaises(schwab.SchwabError, msg=bad):
                schwab.to_schwab(o)


class Tokens(Env):
    def test_fresh_token_is_used_as_is(self):
        fake = FakeSchwab()
        c = self.client(fake)
        c.accounts()
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer AT")

    def test_expired_access_token_is_refreshed_and_saved_privately(self):
        fake = FakeSchwab()
        c = self.client(fake, fresh=False)
        c.accounts()
        tok_call = fake.calls[0]
        self.assertTrue(tok_call[1].endswith("/v1/oauth/token"))
        self.assertIn("grant_type=refresh_token", tok_call[3])
        self.assertTrue(tok_call[2]["Authorization"].startswith("Basic "))
        self.assertEqual(fake.calls[1][2]["Authorization"], "Bearer AT-new")
        saved = schwab.load_token()
        self.assertEqual(saved["access_token"], "AT-new")
        self.assertEqual(saved["refresh_expires_at"], self.now + 86400)   # the 7-day clock does not reset
        self.assertEqual(os.stat(schwab.token_path()).st_mode & 0o777, 0o600)

    def test_after_seven_days_it_asks_to_sign_in_again(self):
        schwab.save_token({"access_token": "AT", "refresh_token": "RT", "expires_at": 0, "refresh_expires_at": 1})
        c = schwab.Client(opener=FakeSchwab(), clock=lambda: self.now)
        with self.assertRaisesRegex(schwab.SchwabError, "schwab.py auth"):
            c.accounts()

    def test_auth_exchanges_the_code_from_the_redirect(self):
        fake = FakeSchwab()
        c = schwab.Client(opener=fake, clock=lambda: self.now)
        with redirect_stdout(io.StringIO()) as out:
            code = schwab.main(["auth", "--redirect-url", "https://127.0.0.1/?code=C0DE%40&session=x"], client=c)
        self.assertEqual(code, 0)
        self.assertIn("code=C0DE%40", fake.calls[0][3])
        self.assertNotIn("AT-new", out.getvalue())   # tokens are never printed
        self.assertEqual(schwab.load_token()["refresh_expires_at"], self.now + 7 * 86400)

    def test_more_than_one_account_needs_a_choice(self):
        fake = FakeSchwab(accounts=[{"accountNumber": "11112222", "hashValue": "A"},
                                    {"accountNumber": "33334444", "hashValue": "B"}])
        c = self.client(fake)
        with self.assertRaisesRegex(schwab.SchwabError, r"\.\.\.2222, \.\.\.4444"):
            c.account_hash()
        self.assertEqual(c.account_hash("4444"), ("B", "33334444"))


class Gate(Env):
    def setUp(self):
        super().setUp()
        t = os.path.join(self.d, "trader")
        os.makedirs(os.path.join(t, "state"))
        self.orders = os.path.join(t, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(t, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(t, "state", "risk.json")
        self.state = os.path.join(t, "state", "broker.json")
        self.pf = os.path.join(t, "portfolio.json")

    def run_risk(self):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", os.path.join(self.d, "none"), "--state", self.state,
                               "--now", NOW, "--out", self.risk])

    def ticket(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ticket.main(list(argv) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                                             "--state", self.state, "--now", NOW])
        return code, out.getvalue(), err.getvalue()

    def place(self, fake):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = schwab.main(["place", "--index", "1", "--orders", self.orders, "--config", self.cfg,
                                "--risk", self.risk, "--state", self.state, "--now", NOW],
                               client=self.client(fake))
        return code, out.getvalue(), err.getvalue()

    def statuses(self):
        with open(self.state) as fh:
            return [r["status"] for r in json.load(fh)["orders"]]

    def test_nothing_goes_out_without_the_risk_check(self):
        fake = FakeSchwab()
        code, _, err = self.place(fake)
        self.assertEqual(code, 1)
        self.assertIn("not placed", err)
        self.assertEqual(fake.posted_orders(), [])

    def test_nothing_goes_out_without_the_users_yes(self):
        self.assertEqual(self.run_risk(), 0)
        fake = FakeSchwab()
        code, _, err = self.place(fake)
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        self.assertEqual(fake.posted_orders(), [])
        code, _, _ = self.ticket("approve", "--index", "1", "--reply", "sure")
        self.assertEqual(code, 1)
        self.assertEqual(self.place(fake)[0], 1)
        self.assertEqual(fake.posted_orders(), [])

    def test_the_ticket_says_live_schwab(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = self.ticket("show", "--index", "1")
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Charles Schwab", text)
        self.assertIn("schwab_place_order", text)
        self.assertIn('"orderStrategyType": "OCO"', text)
        self.assertNotIn("PAPER", text)

    def test_risk_pass_plus_yes_places_exactly_once(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = self.ticket("approve", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 0)
        self.assertIn("schwab_place_order", text)
        fake = FakeSchwab()
        code, text, err = self.place(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("PLACED on Schwab account ...5678 (LIVE)", text)
        self.assertIn("1000123", text)
        self.assertEqual(fake.posted_orders(), [schwab.to_schwab(tsla())])
        self.assertEqual(self.statuses(), ["approved", "placed"])
        with open(self.state) as fh:
            rec = json.load(fh)["orders"][-1]
        self.assertEqual((rec["broker_order_id"], rec["max_loss"], rec["broker"]), ("1000123", 46.88, "schwab"))
        # the same yes never places a second order
        code, _, err = self.place(fake)
        self.assertEqual(code, 1)
        self.assertIn("already placed", err)
        self.assertEqual(len(fake.posted_orders()), 1)

    def test_a_rejected_order_is_recorded_failed_and_not_retried(self):
        self.assertEqual(self.run_risk(), 0)
        self.ticket("approve", "--index", "1", "--reply", "yes")
        code, _, err = self.place(FakeSchwab(place_status=400))
        self.assertEqual(code, 1)
        self.assertIn("insufficient buying power", err)
        self.assertEqual(self.statuses(), ["approved", "failed"])
        fake = FakeSchwab()
        self.assertEqual(self.place(fake)[0], 1)
        self.assertEqual(fake.posted_orders(), [])

    def test_refuses_when_the_config_is_not_schwab(self):
        with open(self.cfg) as fh:
            cfg = json.load(fh)
        cfg["broker"] = "alpaca"
        with open(self.cfg, "w") as fh:
            json.dump(cfg, fh)
        self.assertEqual(self.run_risk(), 0)
        self.ticket("approve", "--index", "1", "--reply", "yes")
        fake = FakeSchwab()
        code, _, err = self.place(fake)
        self.assertEqual(code, 1)
        self.assertIn("broker", err)
        self.assertEqual(fake.posted_orders(), [])

    def test_read_only_commands(self):
        fake = FakeSchwab()
        c = self.client(fake)
        with redirect_stdout(io.StringIO()) as out:
            schwab.main(["account", "--config", self.cfg], client=c)
            schwab.main(["position", "TSLA", "--config", self.cfg], client=c)
            schwab.main(["order", "1000123", "--config", self.cfg], client=c)
        text = out.getvalue()
        self.assertIn("cash: $5,000", text)
        self.assertIn("TSLA 8 shares held", text)
        self.assertIn("ticket.py fill --symbol TSLA --side buy --qty 8 --price 364.24", text)
        self.assertEqual(fake.posted_orders(), [])


class Mcp(Gate):
    def call(self, server, name, args=None, mid=1):
        return server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                              "params": {"name": name, "arguments": args or {}}})["result"]

    def test_handshake_and_tool_list(self):
        s = schwab_mcp.Server(client=self.client(FakeSchwab()), root=self.d)
        init = s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = {t["name"]: t for t in s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertTrue(tools["schwab_place_order"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["schwab_get_account"]["annotations"]["readOnlyHint"])
        self.assertIn("LIVE", tools["schwab_place_order"]["description"])

    def test_place_tool_holds_the_same_gate(self):
        fake = FakeSchwab()
        s = schwab_mcp.Server(client=self.client(fake), root=self.d)
        res = self.call(s, "schwab_place_order", {"index": 1})
        self.assertTrue(res["isError"])
        self.assertEqual(fake.posted_orders(), [])
        res = self.call(s, "schwab_preview_order", {"index": 1})
        self.assertFalse(res["isError"])
        self.assertIn("nothing was sent", res["content"][0]["text"])
        self.assertEqual(fake.posted_orders(), [])

    def test_stdio_loop(self):
        s = schwab_mcp.Server(client=self.client(FakeSchwab()), root=self.d)
        out = io.StringIO()
        schwab_mcp.serve(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n\nnot json\n'), out, s)
        self.assertEqual(json.loads(out.getvalue()), {"jsonrpc": "2.0", "id": 7, "result": {}})


if __name__ == "__main__":
    unittest.main()
