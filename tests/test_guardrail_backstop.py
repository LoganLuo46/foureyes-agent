"""Regression: a guardrail block must be **recorded gracefully** by the graph, not crash it.

Background (failures.md): anyio's TaskGroup wraps McpToolError in an ExceptionGroup, so
execute_action's except never matched -> a guardrail block blew up the whole graph.
This path is the core "human approves by mistake, guardrail catches it" story; pin it down with a test.
Needs postgres + the mcp_action server running.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import graph as G  # noqa: E402
from agent.mcp_client import McpToolError, _unwrap  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def already_refunded_case():
    """An order that was already refunded + a human-approved refund on it still waiting to execute."""
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    ids = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO customers (email,name,tier) VALUES (%s,'Backstop','pro') RETURNING id",
                    (f"bs-{uuid.uuid4().hex[:8]}@t.test",))
        ids["customer"] = cur.fetchone()[0]
        cur.execute("INSERT INTO orders (customer_id,amount_cents,status,placed_at,delivered_at) "
                    "VALUES (%s,19900,'delivered',%s,%s) RETURNING id",
                    (ids["customer"], NOW - timedelta(days=5), NOW - timedelta(days=3)))
        ids["order"] = cur.fetchone()[0]
        # a concurrent ticket already refunded this order
        cur.execute("INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                    "VALUES (%s,%s,'prior','prior','resolved') RETURNING id",
                    (ids["customer"], ids["order"]))
        prior = cur.fetchone()[0]
        ids["prior_ticket"] = prior
        cur.execute("INSERT INTO refunds (ticket_id,order_id,amount_cents,status,idempotency_key,executed_at) "
                    "VALUES (%s,%s,19900,'executed',%s,%s)",
                    (prior, ids["order"], f"bs-prior-{uuid.uuid4().hex[:8]}", NOW))
        # this ticket: a human approved a refund on that same order
        cur.execute("INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                    "VALUES (%s,%s,'t','b','pending_approval') RETURNING id",
                    (ids["customer"], ids["order"]))
        ids["ticket"] = cur.fetchone()[0]
        payload = {"order_id": str(ids["order"]), "amount_cents": 19900,
                   "reason": "human approved", "idempotency_key": f"bs-{uuid.uuid4().hex[:8]}"}
        ids["payload"] = payload
        cur.execute(
            "INSERT INTO approvals (ticket_id,thread_id,action_type,action_payload,agent_reasoning,"
            "evidence,status,decided_by) VALUES (%s,%s,'refund',%s,'r','{}'::jsonb,'approved','human') "
            "RETURNING id", (ids["ticket"], str(ids["ticket"]), json.dumps(payload)))
        ids["approval"] = str(cur.fetchone()[0])
        conn.commit()
    yield conn, ids
    with conn.cursor() as cur:
        for t in (ids["ticket"], ids["prior_ticket"]):
            cur.execute("DELETE FROM audit_log WHERE ticket_id=%s", (t,))
            cur.execute("DELETE FROM refunds WHERE ticket_id=%s", (t,))
            cur.execute("DELETE FROM approvals WHERE ticket_id=%s", (t,))
        for t in (ids["ticket"], ids["prior_ticket"]):
            cur.execute("DELETE FROM tickets WHERE id=%s", (t,))
        cur.execute("DELETE FROM orders WHERE id=%s", (ids["order"],))
        cur.execute("DELETE FROM customers WHERE id=%s", (ids["customer"],))
        conn.commit()
    conn.close()


def test_guardrail_block_is_recorded_not_crashed(already_refunded_case):
    """Human approved, but the guardrail spots the X4 duplicate refund -> execute_action returns blocked, no raise."""
    conn, ids = already_refunded_case
    state = {
        "ticket_id": str(ids["ticket"]),
        "approval_id": ids["approval"],
        "proposed_action": {"type": "refund", "payload": ids["payload"]},
        "tool_calls": [],
    }
    result = G.execute_action(state)  # must not raise
    assert result["execution_result"]["status"] == "blocked"
    assert "business_guardrail" in result["execution_result"]["error"]
    assert "X4" in result["execution_result"]["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refunds WHERE ticket_id=%s", (ids["ticket"],))
        assert cur.fetchone()[0] == 0, "guardrail must prevent the duplicate refund"


def test_verify_and_log_returns_ticket_to_open_after_block(already_refunded_case):
    """After a block, verify_and_log hands the ticket back to a human as open and marks it inconsistent."""
    conn, ids = already_refunded_case
    state = {
        "ticket_id": str(ids["ticket"]),
        "approval_id": ids["approval"],
        "proposed_action": {"type": "refund", "payload": ids["payload"]},
        "tool_calls": [],
    }
    state["execution_result"] = G.execute_action(state)["execution_result"]
    out = G.verify_and_log(state)
    assert out["execution_result"]["verify"]["consistent"] is False
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tickets WHERE id=%s", (ids["ticket"],))
        assert cur.fetchone()[0] == "open"


def test_unwrap_finds_nested_mcp_error():
    """Unit-test the unwrap logic itself (two levels of nested ExceptionGroup)."""
    inner = McpToolError("BLOCKED[business_guardrail] boom")
    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [inner])])
    assert _unwrap(nested) is inner
    assert _unwrap(BaseExceptionGroup("g", [ValueError("x")])) is None
