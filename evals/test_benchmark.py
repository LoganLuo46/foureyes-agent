"""Action-selection benchmark (ADR-011): 80 labeled tickets → both metrics measured for real.

Runs only as far as classify + propose (never reaches the approval gate, never executes), so it **writes nothing**.
Tickets run concurrently to keep wall-clock time sane; each gets its own DB connection and its own data.

The two metrics:
  action_selection_accuracy = share of tickets where the action type was chosen correctly
  false_block_rate          = share of in_policy tickets that should have acted but were refused/escalated instead

  .venv/bin/python evals/test_benchmark.py        # run and print the report
  pytest evals/test_benchmark.py                  # doubles as a regression (asserts only that it ran and produced metrics)
"""

import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent.graph import (classify, gather_evidence, propose_action,  # noqa: E402
                         propose_escalation, route_after_classify, sanitize_input)

BM_DIR = Path(__file__).resolve().parent / "benchmark"
TICKETS = BM_DIR / "tickets.jsonl"          # 80 LLM-generated, then rule-validated
BOUNDARY = BM_DIR / "boundary.jsonl"        # 20 hand-written hard edges (incl. policy-precedence conflicts)
REPORT = BM_DIR / "report.json"
NOW = datetime.now(timezone.utc)
WORKERS = int(os.environ.get("BENCHMARK_WORKERS", "8"))


def load_tickets() -> list[dict]:
    out = []
    for path, subset in ((TICKETS, "generated"), (BOUNDARY, "boundary")):
        if not path.exists():
            continue
        for line in path.open():
            if line.strip():
                t = json.loads(line)
                t["subset"] = subset
                out.append(t)
    return out


def build_case(conn, t: dict) -> tuple[str, list]:
    """Build the account state described by setup; returns (ticket_id, the ids to clean up afterwards)."""
    s = t["setup"]
    created = {"customers": [], "orders": [], "tickets": []}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO customers (email,name,tier) VALUES (%s,'BM User','pro') RETURNING id",
                    (f"bm-{uuid.uuid4().hex[:10]}@eval.test",))
        cust = cur.fetchone()[0]
        created["customers"].append(cust)

        order_id, owner = None, cust
        o = s.get("order")
        if o:
            if s.get("foreign_order"):
                cur.execute("INSERT INTO customers (email,name,tier) VALUES (%s,'Other','free') RETURNING id",
                            (f"bmo-{uuid.uuid4().hex[:10]}@eval.test",))
                owner = cur.fetchone()[0]
                created["customers"].append(owner)
            placed = NOW - timedelta(days=o["placed_days_ago"])
            delivered = (NOW - timedelta(days=max(o["placed_days_ago"] - 2, 0))
                         if o["status"] == "delivered" else None)
            cur.execute("INSERT INTO orders (customer_id,amount_cents,status,placed_at,delivered_at) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (owner, o["amount_cents"], o["status"], placed, delivered))
            order_id = cur.fetchone()[0]
            created["orders"].append(order_id)
            if s.get("prior_refund"):
                # Boundary cases use pending/rejected to check the fine print: R3 only forbids executed refunds
                prior_status = s.get("prior_refund_status", "executed")
                cur.execute("INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                            "VALUES (%s,%s,'prior','prior','resolved') RETURNING id", (owner, order_id))
                pt = cur.fetchone()[0]
                created["tickets"].append(pt)
                cur.execute("INSERT INTO refunds (ticket_id,order_id,amount_cents,status,"
                            "idempotency_key,executed_at) VALUES (%s,%s,%s,%s,%s,%s)",
                            (pt, order_id, o["amount_cents"], prior_status,
                             f"bm-{uuid.uuid4().hex[:8]}",
                             NOW if prior_status == "executed" else None))

        for i in range(s.get("extra_orders", 0)):
            cur.execute("INSERT INTO orders (customer_id,amount_cents,status,placed_at,delivered_at) "
                        "VALUES (%s,%s,'delivered',%s,%s) RETURNING id",
                        (cust, 2500 + i * 1700, NOW - timedelta(days=7 + i), NOW - timedelta(days=5 + i)))
            created["orders"].append(cur.fetchone()[0])

        link = order_id if not s.get("foreign_order") else None
        cur.execute("INSERT INTO tickets (customer_id,order_id,subject,body,status) "
                    "VALUES (%s,%s,%s,%s,'open') RETURNING id",
                    (cust, link, f"[bm {t['id']}]", t["body"]))
        tid = cur.fetchone()[0]
        created["tickets"].append(tid)
        conn.commit()
    return str(tid), created


def cleanup(conn, created: dict):
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


def score_one(t: dict) -> dict:
    """Run sanitize→gather→classify→propose and return the verdict for this one ticket."""
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    created = None
    try:
        ticket_id, created = build_case(conn, t)
        state = {"ticket_id": ticket_id}
        state.update(sanitize_input(state))
        state.update(gather_evidence(state))
        state.update(classify(state))
        route = route_after_classify(state)
        if route == "refuse":
            actual_action = "refuse"
        elif route == "escalate":
            actual_action = propose_escalation(state)["proposed_action"]["type"]
        else:
            actual_action = propose_action(state)["proposed_action"]["type"]
        return {
            "id": t["id"],
            "subset": t.get("subset", "generated"),
            "label": t["label"],
            "expected_action": t["expected_action"],
            "actual_action": actual_action,
            "predicted_label": state.get("classification"),
            "correct": actual_action == t["expected_action"],
            "label_correct": state.get("classification") == t["label"],
            "provider": state.get("llm_provider"),
            "policy_refs": (state.get("classify_result") or {}).get("policy_refs", []),
        }
    except Exception as e:  # noqa: BLE001 — one bad ticket must not take down the whole run
        return {"id": t["id"], "subset": t.get("subset", "generated"), "label": t["label"],
                "expected_action": t["expected_action"],
                "actual_action": None, "correct": False, "label_correct": False,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if created:
            try:
                cleanup(conn, created)
            finally:
                conn.close()
        else:
            conn.close()


def run() -> dict:
    tickets = load_tickets()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, r in enumerate(pool.map(score_one, tickets), 1):
            results.append(r)
            mark = "ok " if r["correct"] else "MISS"
            print(f"[{i:2}/{len(tickets)}] {r['id']} {mark} "
                  f"expected={r['expected_action']:8} actual={r['actual_action']}", flush=True)

    n = len(results)
    correct = sum(r["correct"] for r in results)
    accuracy = correct / n

    # false block: share of in_policy tickets that should have acted (refund/close) but were refused/escalated
    actionable = [r for r in results
                  if r["label"] == "in_policy" and r["expected_action"] in ("refund", "close")]
    blocked = [r for r in actionable if r["actual_action"] in ("refuse", "escalate")]
    false_block_rate = (len(blocked) / len(actionable)) if actionable else 0.0

    per_label = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in results:
        per_label[r["label"]]["n"] += 1
        per_label[r["label"]]["correct"] += int(r["correct"])

    # Report per subset: generated (LLM-written, clear-cut) vs boundary (hand-written hard edges + precedence conflicts)
    per_subset = {}
    for sub in ("generated", "boundary"):
        rows = [r for r in results if r.get("subset") == sub]
        if rows:
            per_subset[sub] = {
                "n": len(rows),
                "correct": sum(r["correct"] for r in rows),
                "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 4),
                "misses": [f"{r['id']}:{r['expected_action']}->{r['actual_action']}"
                           for r in rows if not r["correct"]],
            }

    return {
        "generated_at": NOW.isoformat(),
        "total_tickets": n,
        "action_selection_accuracy": round(accuracy, 4),
        "correct_count": correct,
        "false_block_rate": round(false_block_rate, 4),
        "false_block_numerator": len(blocked),
        "false_block_denominator": len(actionable),
        "false_blocked_ids": [r["id"] for r in blocked],
        "label_accuracy": round(sum(r["label_correct"] for r in results) / n, 4),
        "per_label": {k: {**v, "accuracy": round(v["correct"] / v["n"], 4)} for k, v in per_label.items()},
        "per_subset": per_subset,
        "confusion": dict(Counter(f"{r['expected_action']}->{r['actual_action']}" for r in results)),
        "errors": [r for r in results if r.get("error")],
        "results": results,
    }


def _print(rep: dict):
    print("\n=== BENCHMARK METRICS (measured) ===")
    print(f"action_selection_accuracy : {rep['action_selection_accuracy']:.1%} "
          f"({rep['correct_count']}/{rep['total_tickets']})")
    print(f"false_block_rate          : {rep['false_block_rate']:.1%} "
          f"({rep['false_block_numerator']}/{rep['false_block_denominator']} actionable in_policy tickets)")
    print(f"label_accuracy            : {rep['label_accuracy']:.1%}")
    print(f"per_label                 : {json.dumps(rep['per_label'])}")
    for sub, v in rep.get("per_subset", {}).items():
        print(f"  subset[{sub:9}]        : {v['accuracy']:.1%} ({v['correct']}/{v['n']})"
              + (f"  misses={v['misses']}" if v["misses"] else ""))
    print(f"confusion                 : {json.dumps(rep['confusion'])}")
    if rep["errors"]:
        print(f"errors                    : {len(rep['errors'])}")


def test_benchmark_runs_and_reports():
    """pytest entry point: run the full benchmark and write the report. No thresholds on the metrics — the run decides the numbers."""
    rep = run()
    REPORT.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    _print(rep)
    assert rep["total_tickets"] >= 80, f"spec requires >=80 labeled tickets, got {rep['total_tickets']}"
    assert not rep["errors"], f"{len(rep['errors'])} tickets errored: {rep['errors'][:3]}"


if __name__ == "__main__":
    rep = run()
    REPORT.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    _print(rep)
