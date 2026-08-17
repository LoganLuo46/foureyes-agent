"""Approval console backend (ADR-012).

Four endpoints. Approve/reject **only resume the graph** — this module can execute nothing:
it never imports mcp_action and never holds the action server address (tests/test_topology.py asserts this by scanning).
Endpoints are plain `def`, not async, so FastAPI runs them in a threadpool and the synchronous resume never blocks the event loop.
"""

import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent.graph import graph_session, resume_ticket  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]

app = FastAPI(title="FourEyes Approval Console API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CONSOLE_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


class Decision(BaseModel):
    decided_by: str
    reason: str | None = None


def _db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


APPROVAL_COLUMNS = (
    "a.id, a.ticket_id, a.thread_id, a.action_type, a.action_payload, a.agent_reasoning, "
    "a.status, a.trace_url, a.created_at, a.decided_at, a.decided_by"
)


@app.get("/api/approvals")
def list_approvals(status: str = "pending", limit: int = 50):
    """Pending queue. Carries a ticket/customer summary so the cards need no second request."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {APPROVAL_COLUMNS}, t.subject, t.body, c.name AS customer_name, "
            "c.email AS customer_email, c.tier AS customer_tier "
            "FROM approvals a JOIN tickets t ON t.id = a.ticket_id "
            "JOIN customers c ON c.id = t.customer_id "
            "WHERE a.status = %s ORDER BY a.created_at DESC LIMIT %s",
            (status, limit))
        rows = cur.fetchall()
    return {"approvals": rows, "count": len(rows)}


@app.get("/api/approvals/{approval_id}")
def get_approval(approval_id: str):
    """Detail view: action, amount, agent reasoning, evidence, injection flags, trace link."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {APPROVAL_COLUMNS}, a.evidence, t.subject, t.body, t.status AS ticket_status, "
            "c.name AS customer_name, c.email AS customer_email, c.tier AS customer_tier "
            "FROM approvals a JOIN tickets t ON t.id = a.ticket_id "
            "JOIN customers c ON c.id = t.customer_id WHERE a.id = %s", (approval_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"approval {approval_id} not found")
        # Injection flags come from the approval_requested audit event (the human must see what the agent saw)
        cur.execute(
            "SELECT detail FROM audit_log WHERE ticket_id = %s AND event_type = 'approval_requested' "
            "ORDER BY created_at DESC LIMIT 1", (row["ticket_id"],))
        audit = cur.fetchone()
        row["injection_flags"] = (audit or {}).get("detail", {}).get("injection_flags", [])
    return row


def _decide(approval_id: str, decision: str, body: Decision):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT ticket_id, status FROM approvals WHERE id = %s", (approval_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"approval {approval_id} not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"approval already {row['status']}")

    ticket_id = str(row["ticket_id"])
    # The only thing this does: resume the graph. This layer performs no account operations (ADR-012).
    with graph_session() as graph:
        state = resume_ticket(graph, ticket_id, decision, body.decided_by, body.reason)

    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM tickets WHERE id = %s", (ticket_id,))
        ticket_status = cur.fetchone()["status"]
    return {
        "approval_id": approval_id,
        "ticket_id": ticket_id,
        "decision": decision,
        "execution_result": state.get("execution_result"),
        "ticket_status": ticket_status,
    }


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decision):
    return _decide(approval_id, "approved", body)


@app.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Decision):
    return _decide(approval_id, "rejected", body)


@app.get("/api/healthz")
def healthz():
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM approvals WHERE status='pending'")
        pending = cur.fetchone()["n"]
    return {"ok": True, "pending_approvals": pending}
