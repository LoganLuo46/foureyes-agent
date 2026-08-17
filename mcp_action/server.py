"""FourEyes `ticket-action` MCP server — Python SDK, write-gated.

Three write tools; every entry runs the business guardrails first (mcp_action/guardrails.py, layer 3):
guardrail fails → audit_log(blocked_by='business_guardrail') + raise;
guardrail passes → perform the write + audit_log(event_type='executed').

This server's MCP client may only be instantiated inside the agent graph's execute_action
node (topology constraint 3, asserted by the evals). Transport: Streamable HTTP :8102.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from mcp.server import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from mcp_action.guardrails import check_refund, check_ticket_exists  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]

mcp = MCPServer(
    name="ticket-action",
    version="0.1.0",
    instructions="Write-gated actions on support tickets. Every tool enforces "
    "deterministic business guardrails at entry; violations are audited and rejected.",
)


class GuardrailViolation(Exception):
    """A business guardrail rejected the call. The message cites the policy rule; MCP returns is_error."""


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _audit(cur, ticket_id: str | None, event_type: str, detail: dict, blocked_by: str | None = None) -> None:
    cur.execute(
        "INSERT INTO audit_log (ticket_id, event_type, blocked_by, detail) VALUES (%s,%s,%s,%s)",
        (ticket_id, event_type, blocked_by, json.dumps(detail)),
    )


def _blocked(conn, cur, ticket_id: str | None, tool: str, args: dict, violations: list[str]):
    # ticket_id may not exist, or may be attacker-invented: writing it hits the FK and swallows the audit row.
    # So a fabricated id moves into detail and the audit row's ticket_id goes NULL — a block always gets recorded.
    if ticket_id is not None:
        exists = False
        if _is_uuid(ticket_id):
            cur.execute("SELECT 1 FROM tickets WHERE id = %s", (ticket_id,))
            exists = cur.fetchone() is not None
        if not exists:
            args = {**args, "claimed_ticket_id": str(ticket_id)}
            ticket_id = None
    _audit(cur, ticket_id, "blocked",
           {"tool": tool, "args": args, "violations": violations},
           blocked_by="business_guardrail")
    conn.commit()  # the block must land in the table even though the caller is about to get an exception
    raise GuardrailViolation(f"BLOCKED[business_guardrail] {tool}: " + "; ".join(violations))


@mcp.tool()
def issue_refund(ticket_id: str, order_id: str, amount_cents: int, reason: str, idempotency_key: str) -> dict:
    """Execute a refund for an order. Deterministic guardrails: amount <= order amount,
    amount <= hard cap, order status/window compliant, no prior executed refund,
    unique idempotency_key. Violations are audited and rejected."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        # format check first: a malformed uuid makes SQL throw and the attempt escapes the audit trail
        fmt = [f"malformed {n}: {v!r}" for n, v in (("ticket_id", ticket_id), ("order_id", order_id))
               if not _is_uuid(v)]
        if fmt:
            _blocked(conn, cur, None, "issue_refund",
                     {"ticket_id": str(ticket_id), "order_id": str(order_id)}, fmt)
        violations = check_refund(cur, ticket_id, order_id, amount_cents)
        if violations:
            _blocked(conn, cur, ticket_id, "issue_refund",
                     {"order_id": order_id, "amount_cents": amount_cents, "reason": reason}, violations)
        refund_id = str(uuid.uuid4())
        try:
            cur.execute(
                "INSERT INTO refunds (id, ticket_id, order_id, amount_cents, status, idempotency_key, executed_at) "
                "VALUES (%s,%s,%s,%s,'executed',%s,%s)",
                (refund_id, ticket_id, order_id, amount_cents, idempotency_key,
                 datetime.now(timezone.utc)),
            )
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            with psycopg.connect(DATABASE_URL) as c2, c2.cursor() as cur2:
                _blocked(c2, cur2, ticket_id, "issue_refund",
                         {"order_id": order_id, "idempotency_key": idempotency_key},
                         [f"idempotency_key {idempotency_key!r} already used — duplicate execution refused"])
        cur.execute("UPDATE tickets SET status = 'resolved' WHERE id = %s", (ticket_id,))
        _audit(cur, ticket_id, "executed",
               {"tool": "issue_refund", "refund_id": refund_id, "order_id": order_id,
                "amount_cents": amount_cents, "reason": reason, "idempotency_key": idempotency_key})
        conn.commit()
        return {"refund_id": refund_id, "status": "executed"}


@mcp.tool()
def escalate_ticket(ticket_id: str, reason: str, severity: str = "medium") -> dict:
    """Escalate a ticket to a human specialist. severity: low|medium|high."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        # an illegal severity is a blocked attempt too: send it through _blocked so it audits like every refusal
        if severity not in ("low", "medium", "high"):
            _blocked(conn, cur, ticket_id if _is_uuid(ticket_id) else None, "escalate_ticket",
                     {"ticket_id": str(ticket_id), "severity": severity},
                     [f"invalid severity {severity!r} (low|medium|high)"])
        if not _is_uuid(ticket_id):
            _blocked(conn, cur, None, "escalate_ticket", {"ticket_id": str(ticket_id)},
                     [f"malformed ticket_id: {ticket_id!r}"])
        violations = check_ticket_exists(cur, ticket_id)
        if violations:
            _blocked(conn, cur, None, "escalate_ticket", {"ticket_id": ticket_id}, violations)
        cur.execute("UPDATE tickets SET status = 'escalated' WHERE id = %s", (ticket_id,))
        _audit(cur, ticket_id, "executed",
               {"tool": "escalate_ticket", "reason": reason, "severity": severity})
        conn.commit()
        return {"ticket_id": ticket_id, "status": "escalated"}


@mcp.tool()
def close_ticket(ticket_id: str, resolution_note: str) -> dict:
    """Close a ticket with a resolution note (for pure inquiries needing no account action)."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        if not _is_uuid(ticket_id):
            _blocked(conn, cur, None, "close_ticket", {"ticket_id": str(ticket_id)},
                     [f"malformed ticket_id: {ticket_id!r}"])
        violations = check_ticket_exists(cur, ticket_id)
        if violations:
            _blocked(conn, cur, None, "close_ticket", {"ticket_id": ticket_id}, violations)
        cur.execute("UPDATE tickets SET status = 'resolved' WHERE id = %s", (ticket_id,))
        _audit(cur, ticket_id, "executed",
               {"tool": "close_ticket", "resolution_note": resolution_note})
        conn.commit()
        return {"ticket_id": ticket_id, "status": "resolved"}


if __name__ == "__main__":
    port = int(os.environ.get("MCP_ACTION_PORT", "8102"))
    print(f"ticket-action MCP server (write-gated) on :{port}/mcp")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, stateless_http=True)
