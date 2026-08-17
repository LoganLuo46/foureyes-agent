"""business_guardrail (layer 3) unit tests: every rule checked on its own + idempotency key vs replay.

Calls the guardrail/tool functions directly, not over MCP -- guardrail correctness must not depend on transport.
Needs postgres running (docker compose up -d). The fixture creates its own data and cleans up in teardown.
"""

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

from mcp_action.guardrails import check_refund  # noqa: E402
from mcp_action.server import GuardrailViolation, issue_refund  # noqa: E402

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    created = {"customers": [], "orders": [], "tickets": []}

    def make_case(amount_cents=9900, status="delivered", placed_days_ago=10,
                  executed_refund=False, foreign_customer=False):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (email,name,tier) VALUES (%s,'Test User','free') RETURNING id",
                (f"test-{uuid.uuid4().hex[:8]}@guardrail.test",))
            cust = cur.fetchone()[0]
            created["customers"].append(cust)
            order_owner = cust
            if foreign_customer:
                cur.execute(
                    "INSERT INTO customers (email,name,tier) VALUES (%s,'Other User','free') RETURNING id",
                    (f"other-{uuid.uuid4().hex[:8]}@guardrail.test",))
                order_owner = cur.fetchone()[0]
                created["customers"].append(order_owner)
            cur.execute(
                "INSERT INTO orders (customer_id,amount_cents,status,placed_at,delivered_at) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (order_owner, amount_cents, status, NOW - timedelta(days=placed_days_ago),
                 NOW - timedelta(days=max(placed_days_ago - 2, 0)) if status == "delivered" else None))
            order = cur.fetchone()[0]
            created["orders"].append(order)
            cur.execute(
                "INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                "VALUES (%s,%s,'test','test body','open') RETURNING id", (cust, order))
            ticket = cur.fetchone()[0]
            created["tickets"].append(ticket)
            if executed_refund:
                cur.execute(
                    "INSERT INTO refunds (ticket_id,order_id,amount_cents,status,idempotency_key,executed_at) "
                    "VALUES (%s,%s,%s,'executed',%s,%s)",
                    (ticket, order, 1000, f"test-prior-{uuid.uuid4().hex[:8]}", NOW))
            conn.commit()
            return str(ticket), str(order)

    yield conn, make_case

    with conn.cursor() as cur:
        for t in created["tickets"]:
            cur.execute("DELETE FROM audit_log WHERE ticket_id=%s", (t,))
            cur.execute("DELETE FROM refunds WHERE ticket_id=%s", (t,))
            cur.execute("DELETE FROM approvals WHERE ticket_id=%s", (t,))
        for t in created["tickets"]:
            cur.execute("DELETE FROM tickets WHERE id=%s", (t,))
        for o in created["orders"]:
            cur.execute("DELETE FROM refunds WHERE order_id=%s", (o,))
            cur.execute("DELETE FROM orders WHERE id=%s", (o,))
        for c in created["customers"]:
            cur.execute("DELETE FROM customers WHERE id=%s", (c,))
        conn.commit()
    conn.close()


def _check(conn, ticket, order, amount):
    with conn.cursor() as cur:
        v = check_refund(cur, ticket, order, amount)
        conn.rollback()  # check only, leave no side effects
        return v


def test_eligible_refund_passes(db):
    conn, make = db
    t, o = make()
    assert _check(conn, t, o, 9900) == []


def test_amount_over_order_blocked(db):
    conn, make = db
    t, o = make(amount_cents=5000)
    assert any("X2" in v for v in _check(conn, t, o, 5001))


def test_over_cap_blocked(db):
    conn, make = db
    t, o = make(amount_cents=90000)  # $900 order, $600 asked: over the cap but under the order total
    assert any("R5" in v for v in _check(conn, t, o, 60000))


def test_expired_window_blocked(db):
    conn, make = db
    t, o = make(placed_days_ago=45)
    assert any("X3" in v for v in _check(conn, t, o, 9900))


def test_unshipped_order_blocked(db):
    conn, make = db
    t, o = make(status="paid")
    assert any("R1" in v for v in _check(conn, t, o, 9900))


def test_already_refunded_blocked(db):
    conn, make = db
    t, o = make(executed_refund=True)
    assert any("X4" in v for v in _check(conn, t, o, 9900))


def test_foreign_order_blocked(db):
    conn, make = db
    t, o = make(foreign_customer=True)
    assert any("X1" in v for v in _check(conn, t, o, 9900))


def test_nonexistent_order_blocked(db):
    conn, make = db
    t, _ = make()
    assert any("not exist" in v for v in _check(conn, t, str(uuid.uuid4()), 9900))


def test_nonpositive_amount_blocked(db):
    conn, make = db
    t, o = make()
    assert _check(conn, t, o, 0) and _check(conn, t, o, -500)


def test_issue_refund_happy_path_and_audit(db):
    conn, make = db
    t, o = make()
    result = issue_refund(t, o, 9900, "defective device", f"test-{uuid.uuid4().hex[:8]}")
    assert result["status"] == "executed"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM refunds WHERE id=%s", (result["refund_id"],))
        assert cur.fetchone()[0] == "executed"
        cur.execute("SELECT status FROM tickets WHERE id=%s", (t,))
        assert cur.fetchone()[0] == "resolved"
        cur.execute("SELECT count(*) FROM audit_log WHERE ticket_id=%s AND event_type='executed'", (t,))
        assert cur.fetchone()[0] == 1


def test_duplicate_idempotency_key_refused_and_audited(db):
    conn, make = db
    t, o = make(amount_cents=20000)
    key = f"test-dup-{uuid.uuid4().hex[:8]}"
    issue_refund(t, o, 5000, "first", key)
    # a second refund on the same order would hit X4 first; use a fresh case with the same key to test the constraint
    t2, o2 = make()
    with pytest.raises(GuardrailViolation, match="idempotency_key"):
        issue_refund(t2, o2, 5000, "replay", key)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE ticket_id=%s AND event_type='blocked' "
            "AND blocked_by='business_guardrail'", (t2,))
        assert cur.fetchone()[0] == 1


def test_nonexistent_ticket_block_is_still_audited(db):
    """Regression: a made-up ticket_id used to break the audit write on the FK -- blocked, but no audit trail."""
    conn, _ = db
    fake_ticket, fake_order = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(GuardrailViolation):
        issue_refund(fake_ticket, fake_order, 100, "attack", f"test-{uuid.uuid4().hex[:8]}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM audit_log WHERE ticket_id IS NULL AND blocked_by='business_guardrail' "
            "AND detail->'args'->>'claimed_ticket_id' = %s", (fake_ticket,))
        row = cur.fetchone()
        assert row, "blocked attempt on fake ticket must be audited with NULL ticket_id"
        cur.execute("DELETE FROM audit_log WHERE id=%s", (row[0],))
        conn.commit()


def test_malformed_uuid_blocked_not_crashed(db):
    """A malformed uuid must take the guardrail refusal path, not surface as a raw SQL exception."""
    conn, _ = db
    with pytest.raises(GuardrailViolation, match="malformed"):
        issue_refund("'; DROP TABLE refunds;--", "also-not-a-uuid", 100, "x",
                     f"test-{uuid.uuid4().hex[:8]}")
    with conn.cursor() as cur:  # clean up that NULL-ticket audit row
        cur.execute(
            "DELETE FROM audit_log WHERE ticket_id IS NULL AND blocked_by='business_guardrail' "
            "AND detail->'violations'->>0 LIKE 'malformed%'")
        conn.commit()


def test_guardrail_block_writes_audit(db):
    conn, make = db
    t, o = make(placed_days_ago=60)
    with pytest.raises(GuardrailViolation, match="X3"):
        issue_refund(t, o, 9900, "too late", f"test-{uuid.uuid4().hex[:8]}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM audit_log WHERE ticket_id=%s AND blocked_by='business_guardrail'", (t,))
        rows = cur.fetchall()
        assert rows, "blocked attempt must be audited"
