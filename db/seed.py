"""LLM-generated synthetic tickets → loaded into the database (ADR-003).

First run: call Claude to generate the data → cache it at db/seed_data.json (committed to git).
After that: load straight from the cache. Deterministic, no API dependency.
Dates are relative day offsets resolved at load time, so time-window scenarios never drift with the calendar.

Usage:
  .venv/bin/python db/seed.py            # refuses if tickets already exist (guards against a stray reload)
  .venv/bin/python db/seed.py --reset    # truncate the business tables, then reload
  .venv/bin/python db/seed.py --regenerate  # force a fresh LLM generation (overwrites the cache)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CACHE_PATH = Path(__file__).resolve().parent / "seed_data.json"

# Ticket scenario categories (aligned with policy/support_policy.md) and target counts
CATEGORY_PLAN = {
    "refund_eligible": 12,   # R1-R5 all satisfied → in_policy/refund
    "over_cap": 6,           # over $500 → in_policy/escalate (E1)
    "expired_window": 6,     # past 30 days → out_of_policy (X3)
    "already_refunded": 5,   # refunded once already → out_of_policy (X4)
    "no_order": 5,           # no such order → out_of_policy (X1)
    "over_amount": 5,        # asks for more than the order total → out_of_policy (X2)
    "under_specified": 9,    # not enough information → under_specified (E3)
    "inquiry": 8,            # pure question → in_policy/close (C1)
    "safety_legal": 4,       # safety/legal → in_policy/escalate (E2)
}

SEED_SCHEMA = {
    "type": "object",
    "properties": {
        "customers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "tier": {"type": "string", "enum": ["free", "pro", "enterprise"]},
                },
                "required": ["key", "email", "name", "tier"],
                "additionalProperties": False,
            },
        },
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "customer_key": {"type": "string"},
                    "amount_cents": {"type": "integer"},
                    "status": {"type": "string", "enum": ["paid", "shipped", "delivered", "cancelled"]},
                    "placed_days_ago": {"type": "integer"},
                    "delivered_days_ago": {"type": ["integer", "null"]},
                },
                "required": ["key", "customer_key", "amount_cents", "status",
                             "placed_days_ago", "delivered_days_ago"],
                "additionalProperties": False,
            },
        },
        "executed_refunds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order_key": {"type": "string"},
                    "amount_cents": {"type": "integer"},
                    "days_ago": {"type": "integer"},
                },
                "required": ["order_key", "amount_cents", "days_ago"],
                "additionalProperties": False,
            },
        },
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "customer_key": {"type": "string"},
                    "order_key": {"type": ["string", "null"]},
                    "category": {"type": "string", "enum": list(CATEGORY_PLAN.keys())},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["customer_key", "order_key", "category", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["customers", "orders", "executed_refunds", "tickets"],
    "additionalProperties": False,
}

GENERATION_PROMPT = f"""You are generating synthetic test data for a customer-support ticket system (it will be publicly declared as LLM-generated).
Business context: an e-commerce store selling smart-home devices (speakers, cameras, sensors, hubs, etc., $15-$1500 each).

Generate:
- 14 customers (key: c1..c14), with varied names/emails (names from different cultural backgrounds); tier skewed toward free, few enterprise
- about 32 orders (key: o1..o32), amounts as integer cents, with statuses and dates that can support the ticket scenarios below
- executed_refunds: create already-executed refund records for the orders used by the "already_refunded" scenario
- ticket counts per category: {json.dumps(CATEGORY_PLAN, ensure_ascii=False)}

Ticket body requirements (important):
- English, written like a real person: some polite, some furious, some rambling, some one-liners, occasional typos, varying length (20-200 words)
- The body must **not** state which category it belongs to; let the facts show it (orders can be referenced like "order o7")
- refund_eligible: order delivered/shipped, placed ≤28 days ago, amount ≤$500, no prior refund, customer clearly asks for a refund
- over_cap: order is otherwise compliant but the amount is >$500, customer wants a full refund
- expired_window: placed 45-120 days ago, customer wants a refund
- already_refunded: the order is in executed_refunds and the customer comes back asking for another refund
- no_order: the order the customer mentions does not exist (order_key is null; the body may invent a nonexistent order number)
- over_amount: the customer asks for an amount clearly above the order total (write the requested amount into the body)
- under_specified: the customer wants a refund but does not say which order / has several orders and it cannot be pinned down / key information is missing (order_key is null)
- inquiry: a pure question (warranty policy, shipping times, how to pair a device) that needs no account action
- safety_legal: a device caught fire or smoked, someone was injured, threats to sue, and so on; should be escalated to a human

Hard consistency constraints:
- Every ticket's customer_key/order_key must really exist and the order must belong to that customer (except no_order/under_specified, where order_key=null)
- expired_window orders have placed_days_ago between 45 and 120; refund_eligible between 3 and 28
- Orders with status delivered have a non-null delivered_days_ago that is < placed_days_ago; undelivered orders have null
- over_cap order amounts are 50001-150000 cents; most other orders fall in 1500-50000
"""


def generate_via_llm() -> dict:
    import anthropic

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    print("Calling Claude (claude-opus-5) to generate synthetic seed data...")
    with client.messages.stream(
        model="claude-opus-5",
        max_tokens=64000,
        output_config={
            "effort": "low",  # data generation needs no deep reasoning; low effort is faster and cheaper
            "format": {"type": "json_schema", "schema": SEED_SCHEMA},
        },
        messages=[{"role": "user", "content": GENERATION_PROMPT}],
    ) as stream:
        response = stream.get_final_message()
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    print(f"Generated: {len(data['customers'])} customers, {len(data['orders'])} orders, "
          f"{len(data['executed_refunds'])} executed refunds, {len(data['tickets'])} tickets")
    return data


def validate(data: dict) -> list[str]:
    """Light consistency checks; LLM output is not to be trusted blindly."""
    errors = []
    customer_keys = {c["key"] for c in data["customers"]}
    orders = {o["key"]: o for o in data["orders"]}
    refunded_keys = {r["order_key"] for r in data["executed_refunds"]}

    for o in data["orders"]:
        if o["customer_key"] not in customer_keys:
            errors.append(f"order {o['key']}: unknown customer {o['customer_key']}")
        if o["status"] == "delivered" and o["delivered_days_ago"] is None:
            errors.append(f"order {o['key']}: delivered but no delivered_days_ago")
    for r in data["executed_refunds"]:
        if r["order_key"] not in orders:
            errors.append(f"refund on unknown order {r['order_key']}")
    for i, t in enumerate(data["tickets"]):
        if t["customer_key"] not in customer_keys:
            errors.append(f"ticket[{i}]: unknown customer {t['customer_key']}")
        ok = t["order_key"]
        if ok is not None:
            if ok not in orders:
                errors.append(f"ticket[{i}]: unknown order {ok}")
            elif orders[ok]["customer_key"] != t["customer_key"]:
                errors.append(f"ticket[{i}]: order {ok} belongs to someone else")
        if t["category"] == "already_refunded" and ok not in refunded_keys:
            errors.append(f"ticket[{i}]: already_refunded but order {ok} has no executed refund")
        if t["category"] == "expired_window" and ok in orders and not (45 <= orders[ok]["placed_days_ago"] <= 120):
            errors.append(f"ticket[{i}]: expired_window but order placed {orders[ok]['placed_days_ago']}d ago")
        if t["category"] == "refund_eligible" and ok in orders:
            o = orders[ok]
            if o["placed_days_ago"] > 28 or o["amount_cents"] > 50000 or o["status"] not in ("delivered", "shipped") or ok in refunded_keys:
                errors.append(f"ticket[{i}]: refund_eligible violated by order {ok}")
        if t["category"] == "over_cap" and ok in orders and orders[ok]["amount_cents"] <= 50000:
            errors.append(f"ticket[{i}]: over_cap but order amount {orders[ok]['amount_cents']} <= 50000")
    return errors


def load_or_generate(regenerate: bool) -> dict:
    if CACHE_PATH.exists() and not regenerate:
        print(f"Loading cached seed data from {CACHE_PATH}")
        return json.loads(CACHE_PATH.read_text())
    data = generate_via_llm()
    errors = validate(data)
    if errors:
        print("Consistency errors in generated data:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    CACHE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Cached to {CACHE_PATH}")
    return data


def insert(data: dict, reset: bool) -> None:
    now = datetime.now(timezone.utc)
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tickets")
        existing = cur.fetchone()[0]
        if existing and not reset:
            print(f"tickets already has {existing} rows; use --reset to truncate and reload.", file=sys.stderr)
            sys.exit(1)
        if reset:
            cur.execute("TRUNCATE audit_log, approvals, refunds, tickets, orders, customers CASCADE")
            print("Truncated business tables.")

        cust_ids, order_ids = {}, {}
        for c in data["customers"]:
            cur.execute(
                "INSERT INTO customers (email, name, tier, created_at) VALUES (%s,%s,%s,%s) RETURNING id",
                (c["email"], c["name"], c["tier"], now - timedelta(days=365)),
            )
            cust_ids[c["key"]] = cur.fetchone()[0]
        for o in data["orders"]:
            placed = now - timedelta(days=o["placed_days_ago"])
            delivered = now - timedelta(days=o["delivered_days_ago"]) if o["delivered_days_ago"] is not None else None
            cur.execute(
                "INSERT INTO orders (customer_id, amount_cents, status, placed_at, delivered_at) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (cust_ids[o["customer_key"]], o["amount_cents"], o["status"], placed, delivered),
            )
            order_ids[o["key"]] = cur.fetchone()[0]

        # An executed refund needs a historical ticket to hang off (refunds.ticket_id is NOT NULL)
        for i, r in enumerate(data["executed_refunds"]):
            order = next(o for o in data["orders"] if o["key"] == r["order_key"])
            cur.execute(
                "INSERT INTO tickets (customer_id, order_id, subject, body, status, created_at) "
                "VALUES (%s,%s,%s,%s,'resolved',%s) RETURNING id",
                (cust_ids[order["customer_key"]], order_ids[r["order_key"]],
                 "Refund request (resolved)",
                 "[historical ticket] Refund was requested and executed.",
                 now - timedelta(days=r["days_ago"] + 1)),
            )
            hist_ticket = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO refunds (ticket_id, order_id, amount_cents, status, idempotency_key, created_at, executed_at) "
                "VALUES (%s,%s,%s,'executed',%s,%s,%s)",
                (hist_ticket, order_ids[r["order_key"]], r["amount_cents"],
                 f"seed-refund-{r['order_key']}-{i}",
                 now - timedelta(days=r["days_ago"] + 1), now - timedelta(days=r["days_ago"])),
            )

        for t in data["tickets"]:
            cur.execute(
                "INSERT INTO tickets (customer_id, order_id, subject, body, status, created_at) "
                "VALUES (%s,%s,%s,%s,'open',%s)",
                (cust_ids[t["customer_key"]],
                 order_ids.get(t["order_key"]) if t["order_key"] else None,
                 t["subject"], t["body"], now - timedelta(hours=len(t["subject"]) % 72)),
            )
        conn.commit()

        for table in ("customers", "orders", "refunds", "tickets"):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - table names are literals
            print(f"  {table}: {cur.fetchone()[0]} rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="truncate business tables first")
    ap.add_argument("--regenerate", action="store_true", help="force regeneration via LLM")
    args = ap.parse_args()
    seed_data = load_or_generate(args.regenerate)
    insert(seed_data, args.reset)
    print("Seed complete.")
