"""Regression: consent must be bound to the action that actually executes (HIGH finding from adversarial review).

The invariant: execute_action runs the action recorded on **the approval row the human signed**,
not whatever mutable graph state says. Human approves A while the system does B must be impossible.

These tests call node functions directly (no full graph), building states where the approval row and
the proposal disagree; execute_action must refuse and log structural_layer. Needs postgres + mcp_action server.
"""

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent import graph as G  # noqa: E402


@pytest.fixture()
def ticket_with_order():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    ids = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO customers (email,name,tier) VALUES (%s,'CB','free') RETURNING id",
                    (f"cb-{uuid.uuid4().hex[:8]}@t.test",))
        ids["customer"] = cur.fetchone()[0]
        cur.execute("INSERT INTO orders (customer_id,amount_cents,status,placed_at,delivered_at) "
                    "VALUES (%s,9900,'delivered',now()-interval '5 days',now()-interval '3 days') RETURNING id",
                    (ids["customer"],))
        ids["order"] = cur.fetchone()[0]
        cur.execute("INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                    "VALUES (%s,%s,'t','b','pending_approval') RETURNING id",
                    (ids["customer"], ids["order"]))
        ids["ticket"] = cur.fetchone()[0]
        conn.commit()
    yield conn, ids
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log WHERE ticket_id=%s", (ids["ticket"],))
        cur.execute("DELETE FROM refunds WHERE ticket_id=%s", (ids["ticket"],))
        cur.execute("DELETE FROM approvals WHERE ticket_id=%s", (ids["ticket"],))
        cur.execute("DELETE FROM tickets WHERE id=%s", (ids["ticket"],))
        cur.execute("DELETE FROM orders WHERE id=%s", (ids["order"],))
        cur.execute("DELETE FROM customers WHERE id=%s", (ids["customer"],))
        conn.commit()
    conn.close()


def _insert_approval(conn, ticket_id, action_type, payload, status="approved"):
    import json
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO approvals (ticket_id,thread_id,action_type,action_payload,agent_reasoning,"
            "evidence,status,decided_by) VALUES (%s,%s,%s,%s,'r','{}'::jsonb,%s,'human') RETURNING id",
            (ticket_id, str(ticket_id), action_type, json.dumps(payload), status))
        aid = str(cur.fetchone()[0])
        conn.commit()
    return aid


def test_execute_runs_the_approved_action_not_graph_state(ticket_with_order):
    """Human approved escalate but graph state was flipped to refund -> refuse, log structural_layer."""
    conn, ids = ticket_with_order
    approved = _insert_approval(conn, ids["ticket"], "escalate", {"reason": "x", "severity": "medium"})
    # graph state lies that the proposal is a refund (simulates replay/tampering splitting consent from action)
    state = {
        "ticket_id": str(ids["ticket"]),
        "approval_id": approved,
        "proposed_action": {"type": "refund",
                            "payload": {"order_id": str(ids["order"]), "amount_cents": 9900,
                                        "reason": "x", "idempotency_key": f"cb-{uuid.uuid4().hex[:8]}"}},
        "tool_calls": [],
    }
    result = G.execute_action(state)
    assert result["execution_result"]["status"] == "blocked"
    assert "does not match" in result["execution_result"]["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refunds WHERE ticket_id=%s", (ids["ticket"],))
        assert cur.fetchone()[0] == 0, "no refund may execute when consent was for escalate"
        cur.execute("SELECT count(*) FROM audit_log WHERE ticket_id=%s AND blocked_by='structural_layer'",
                    (ids["ticket"],))
        assert cur.fetchone()[0] >= 1, "mismatch must be audited as structural_layer block"


def test_execute_refuses_unapproved_row(ticket_with_order):
    """The approval row is still pending (nobody approved) -> execute must refuse to run it."""
    conn, ids = ticket_with_order
    pending = _insert_approval(conn, ids["ticket"], "escalate", {"reason": "x", "severity": "low"},
                               status="pending")
    state = {
        "ticket_id": str(ids["ticket"]),
        "approval_id": pending,
        "proposed_action": {"type": "escalate", "payload": {"reason": "x", "severity": "low"}},
        "tool_calls": [],
    }
    result = G.execute_action(state)
    assert result["execution_result"]["status"] == "blocked"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tickets WHERE id=%s", (ids["ticket"],))
        assert cur.fetchone()[0] == "pending_approval", "unapproved row must not drive execution"


def test_execute_runs_matching_approved_action(ticket_with_order):
    """Escalate approved and graph state agrees -> it executes (proves the gate isn't just blocking everything)."""
    conn, ids = ticket_with_order
    payload = {"reason": "safety concern", "severity": "high"}
    approved = _insert_approval(conn, ids["ticket"], "escalate", payload)
    state = {
        "ticket_id": str(ids["ticket"]),
        "approval_id": approved,
        "proposed_action": {"type": "escalate", "payload": payload},
        "tool_calls": [],
    }
    result = G.execute_action(state)
    assert result["execution_result"]["status"] == "executed"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tickets WHERE id=%s", (ids["ticket"],))
        assert cur.fetchone()[0] == "escalated"


def test_request_approval_supersedes_stale_pending(ticket_with_order):
    """A pending escalate exists but the new proposal is a refund -> void the old row, never reuse it."""
    conn, ids = ticket_with_order
    stale = _insert_approval(conn, ids["ticket"], "escalate", {"reason": "x", "severity": "low"},
                             status="pending")
    state = {
        "ticket_id": str(ids["ticket"]),
        "proposed_action": {"type": "refund",
                            "payload": {"order_id": str(ids["order"]), "amount_cents": 9900,
                                        "reason": "y", "idempotency_key": "k"}},
        "evidence": {},
    }
    out = G.request_approval(state)
    assert out["approval_id"] != stale, "must not reuse a pending row whose action differs"
    with conn.cursor() as cur:
        cur.execute("SELECT status, decided_by FROM approvals WHERE id=%s", (stale,))
        assert cur.fetchone() == ("rejected", "system:superseded")
        cur.execute("SELECT action_type FROM approvals WHERE id=%s", (out["approval_id"],))
        assert cur.fetchone()[0] == "refund"
