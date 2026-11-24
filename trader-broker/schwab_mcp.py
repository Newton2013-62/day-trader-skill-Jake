#!/usr/bin/env python3
"""A local MCP server (stdio) that gives Claude Schwab tools. Standard library only.

It runs on your computer, reads your Schwab sign-in from ~/.schwab-trader
(made by `schwab.py auth`), and works on the trader/ folder in TRADER_DIR
(default: the folder Claude starts it in).

Tools:
  schwab_get_account     cash, buying power, positions            (read only)
  schwab_get_orders      the last day's orders                    (read only)
  schwab_get_order       one order's status and fills             (read only)
  schwab_preview_order   the exact Schwab JSON for one ticket     (read only, sends nothing)
  schwab_place_order     LIVE: places one ticket, only if trader-risk passed it today AND
                         the user's yes for it is recorded (ticket.py approve)
  schwab_cancel_order    cancels one open order

Set schwab_place_order and schwab_cancel_order to "Needs approval" (Claude Desktop) or
list them under "ask" in .claude/settings.json (Claude Code). Never "Always allow".
"""

import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schwab  # noqa: E402
import ticket  # noqa: E402

PROTOCOL = "2024-11-05"


def _idx():
    return {"type": "object", "required": ["index"], "additionalProperties": False,
            "properties": {"index": {"type": "integer", "minimum": 1,
                                     "description": "the ticket number from ticket.py show"}}}


def _oid():
    return {"type": "object", "required": ["order_id"], "additionalProperties": False,
            "properties": {"order_id": {"type": "string", "description": "Schwab's order id"}}}


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = [
    {"name": "schwab_get_account", "description": "Schwab account: cash, buying power and positions. Read only.",
     "inputSchema": EMPTY, "annotations": {"readOnlyHint": True}},
    {"name": "schwab_get_orders", "description": "Schwab orders from the last day, with fills. Read only.",
     "inputSchema": EMPTY, "annotations": {"readOnlyHint": True}},
    {"name": "schwab_get_order", "description": "One Schwab order's status and fills. Read only.",
     "inputSchema": _oid(), "annotations": {"readOnlyHint": True}},
    {"name": "schwab_preview_order",
     "description": "Shows the exact Schwab order JSON for ticket N in trader/orders.json. Sends nothing.",
     "inputSchema": _idx(), "annotations": {"readOnlyHint": True}},
    {"name": "schwab_place_order",
     "description": "LIVE, REAL MONEY. Places ticket N from trader/orders.json on Schwab. Refuses unless "
                    "trader-risk passed that exact order today and the user's explicit yes for it was "
                    "recorded with ticket.py approve. One order per call; never retried automatically.",
     "inputSchema": _idx(), "annotations": {"readOnlyHint": False, "destructiveHint": True,
                                            "idempotentHint": False, "openWorldHint": True}},
    {"name": "schwab_cancel_order", "description": "Cancels one open Schwab order by its id.",
     "inputSchema": _oid(), "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


class Server:
    def __init__(self, client=None, root=None):
        self.client = client or schwab.Client()
        self.root = root or os.environ.get("TRADER_DIR") or os.getcwd()

    def path(self, *parts):
        return os.path.join(self.root, "trader", *parts)

    def cfg(self):
        return ticket.read_json(self.path("config.json"), "config", required=False) or {}

    def run_tool(self, name, args):
        c = self.client
        if name == "schwab_get_account":
            h, number = c.account_hash(schwab.wanted_account(self.cfg()))
            return schwab.summarize_account(c.account(h), number)
        if name == "schwab_get_orders":
            h, _ = c.account_hash(schwab.wanted_account(self.cfg()))
            rows = c.orders(h)
            return "\n".join(schwab.summarize_order(o) for o in rows) or "No Schwab orders in the last day."
        if name == "schwab_get_order":
            h, _ = c.account_hash(schwab.wanted_account(self.cfg()))
            return schwab.summarize_order(c.order(h, args["order_id"]))
        if name == "schwab_preview_order":
            out = io.StringIO()
            with redirect_stdout(out):
                schwab.main(["preview", "--index", str(int(args["index"])), "--orders", self.path("orders.json"),
                             "--config", self.path("config.json")], client=c)
            return out.getvalue().strip()
        if name == "schwab_place_order":
            return schwab.place_approved(c, self.path("orders.json"), self.path("config.json"),
                                         self.path("state", "risk.json"), self.path("state", "broker.json"),
                                         int(args["index"]))
        if name == "schwab_cancel_order":
            h, _ = c.account_hash(schwab.wanted_account(self.cfg()))
            c.cancel(h, args["order_id"])
            return "Asked Schwab to cancel order %s." % args["order_id"]
        raise schwab.SchwabError("unknown tool %s" % name)

    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None  # a notification
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            return _ok(mid, {"protocolVersion": ver, "capabilities": {"tools": {}},
                             "serverInfo": {"name": "schwab-trader", "version": "1.0.0"}})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = self.run_tool(p.get("name"), p.get("arguments") or {})
                return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except (schwab.SchwabError, ticket.TicketError, KeyError, ValueError) as e:
                return _ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method %s" % method}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout, server=None):
    server = server or Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
