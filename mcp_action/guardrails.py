"""Layer 3 defense: business guardrails — deterministic checks no model and no human can talk around (ADR-005).

Each check returns its own violation string (citing a policy rule ID), which keeps audits and tests readable.
Caller contract: inside one transaction, lock the order row FOR UPDATE first (check_refund does this itself),
then write immediately once the checks pass — transaction serialization is what stops concurrent double refunds.
"""

import os
from datetime import datetime, timedelta, timezone

REFUND_CAP_CENTS = int(os.environ.get("REFUND_CAP_CENTS", "50000"))
REFUND_WINDOW_DAYS = int(os.environ.get("REFUND_WINDOW_DAYS", "30"))


def check_refund(cur, ticket_id: str, order_id: str, amount_cents: int) -> list[str]:
    """Refund guardrails. Returns the list of violations; an empty list means allow.

    cur: a psycopg cursor with a transaction already open (the checks take a FOR UPDATE lock on the order row).
    """
    violations: list[str] = []

    if amount_cents <= 0:
        return [f"amount_cents must be positive, got {amount_cents}"]

    cur.execute("SELECT customer_id FROM tickets WHERE id = %s", (ticket_id,))
    trow = cur.fetchone()
    if trow is None:
        return [f"ticket {ticket_id} does not exist"]
    ticket_customer = trow[0]

    # row lock: a concurrent second refund request waits here, and once through it sees the first one executed
    cur.execute(
        "SELECT customer_id, amount_cents, status, placed_at FROM orders WHERE id = %s FOR UPDATE",
        (order_id,),
    )
    orow = cur.fetchone()
    if orow is None:
        return [f"order {order_id} does not exist (policy X1)"]
    order_customer, order_amount, order_status, placed_at = orow

    if order_customer != ticket_customer:
        violations.append(f"order {order_id} does not belong to ticket customer (policy X1)")
    if amount_cents > order_amount:
        violations.append(
            f"refund {amount_cents} exceeds order amount {order_amount} (policy X2 / R4)")
    if amount_cents > REFUND_CAP_CENTS:
        violations.append(
            f"refund {amount_cents} exceeds hard cap {REFUND_CAP_CENTS} (policy R5)")
    if order_status not in ("delivered", "shipped"):
        violations.append(f"order status {order_status!r} not refundable (policy R1)")
    if placed_at < datetime.now(timezone.utc) - timedelta(days=REFUND_WINDOW_DAYS):
        violations.append(
            f"order placed {placed_at.date()} outside {REFUND_WINDOW_DAYS}-day window (policy X3 / R2)")

    cur.execute(
        "SELECT count(*) FROM refunds WHERE order_id = %s AND status = 'executed'",
        (order_id,),
    )
    if cur.fetchone()[0] > 0:
        violations.append(f"order {order_id} already has an executed refund (policy X4 / R3)")

    return violations


def check_ticket_exists(cur, ticket_id: str) -> list[str]:
    cur.execute("SELECT 1 FROM tickets WHERE id = %s", (ticket_id,))
    if cur.fetchone() is None:
        return [f"ticket {ticket_id} does not exist"]
    return []
