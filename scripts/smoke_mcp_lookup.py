"""mcp_lookup connectivity smoke: list_tools + real-data calls (acceptance checklist item 3, automated)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
URL = os.environ.get("MCP_LOOKUP_URL", "http://localhost:8101/mcp")


async def main() -> None:
    # Pull a real ticket out of the DB to use as test input
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, customer_id, order_id FROM tickets WHERE status='open' AND order_id IS NOT NULL LIMIT 1")
        ticket_id, customer_id, order_id = cur.fetchone()

    async with streamable_http_client(URL) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            expected = ["get_customer", "get_order", "get_refund_history",
                        "get_ticket", "list_customer_orders", "search_policy"]
            assert names == expected, f"tool list mismatch: {names}"

            r = await session.call_tool("get_ticket", {"ticket_id": str(ticket_id)})
            t = json.loads(r.content[0].text)
            assert "untrusted_content" in t["body"], "ticket body must be wrapped as untrusted"
            print(f"get_ticket ok: subject={t['subject']['untrusted_content'][:40]!r}")

            r = await session.call_tool("get_order", {"order_id": str(order_id)})
            o = json.loads(r.content[0].text)
            assert isinstance(o["amount_cents"], int)
            print(f"get_order ok: amount_cents={o['amount_cents']} status={o['status']}")

            r = await session.call_tool("list_customer_orders", {"customer_id": str(customer_id), "limit": 5})
            print(f"list_customer_orders ok: {len(json.loads(r.content[0].text))} orders")

            r = await session.call_tool("get_refund_history", {"customer_id": str(customer_id)})
            print(f"get_refund_history ok: {len(json.loads(r.content[0].text))} refunds")

            r = await session.call_tool("search_policy", {"query": "refund window 30 days"})
            hits = json.loads(r.content[0].text)
            assert hits, "policy search returned nothing"
            print(f"search_policy ok: top={hits[0]['heading']!r} score={hits[0]['score']}")

            # Bad input must not take the server down
            r = await session.call_tool("get_ticket", {"ticket_id": "not-a-uuid"})
            assert r.is_error or "error" in r.content[0].text
            print("invalid-input handling ok")

    print("SMOKE PASS: mcp_lookup")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"SMOKE FAIL: {e}", file=sys.stderr)
        sys.exit(1)
