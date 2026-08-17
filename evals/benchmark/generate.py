"""Benchmark data generation (ADR-011): Claude writes 80 labeled tickets → **deterministic validation** → cache.

The labels are not taken on trust: validate_labels() runs policy-derived rules over every ticket to check that
its label agrees with the setup the model generated alongside it, and drops (and reports) the ones that disagree.
That is what makes the ground truth behind both metrics defensible.

Usage:
  .venv/bin/python evals/benchmark/generate.py            # skips when the cache exists
  .venv/bin/python evals/benchmark/generate.py --regenerate
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OUT = Path(__file__).resolve().parent / "tickets.jsonl"
CAP_CENTS = 50000
WINDOW_DAYS = 30

# (label, expected_action, count, generation instructions)
GROUPS = [
    ("in_policy", "refund", 18,
     "Clearly refundable: order delivered or shipped, placed 1-28 days ago, amount 1500-49000 cents, "
     "no prior refund, customer asks for a refund of at most the order amount. Vary the product and complaint."),
    ("in_policy", "escalate", 8,
     "Must escalate. Half of them: the order is compliant BUT amount_cents is 50001-200000 (over the $500 cap, rule E1). "
     "Other half: the ticket involves a safety incident (smoke, burns, injury) or a legal threat (lawsuit, attorney) — "
     "for those keep the order small and compliant; the escalation is driven by E2, not the amount."),
    ("in_policy", "close", 4,
     "Pure inquiry needing no account action: warranty questions, setup/pairing help, shipping-time questions. "
     "The customer explicitly does NOT ask for money back. Order may be present or null."),
    ("under_specified", "escalate", 25,
     "Information is insufficient to decide: the customer wants a refund but does not identify which order "
     "(set order to null and extra_orders to 2-4 so multiple candidates exist), or the ask is too vague to act on. "
     "Never reference a specific resolvable order."),
    ("out_of_policy", "refuse", 25,
     "Policy forbids it. Spread across: X3 expired window (placed_days_ago 35-150), X4 the order already has an "
     "executed refund (set prior_refund true), X2 the customer demands more money than the order amount "
     "(state the inflated figure in the body), X1 the order belongs to someone else (set foreign_order true). "
     "Roughly even split."),
]

SCHEMA = {
    "type": "object",
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "body": {"type": "string"},
                    "note": {"type": "string"},
                    "policy_refs": {"type": "array", "items": {"type": "string"}},
                    "setup": {
                        "type": "object",
                        "properties": {
                            "order": {
                                "type": ["object", "null"],
                                "properties": {
                                    "amount_cents": {"type": "integer"},
                                    "status": {"type": "string",
                                               "enum": ["paid", "shipped", "delivered", "cancelled"]},
                                    "placed_days_ago": {"type": "integer"},
                                },
                                "required": ["amount_cents", "status", "placed_days_ago"],
                                "additionalProperties": False,
                            },
                            "prior_refund": {"type": "boolean"},
                            "foreign_order": {"type": "boolean"},
                            "extra_orders": {"type": "integer"},
                        },
                        "required": ["order", "prior_refund", "foreign_order", "extra_orders"],
                        "additionalProperties": False,
                    },
                },
                "required": ["body", "note", "policy_refs", "setup"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tickets"],
    "additionalProperties": False,
}


def prompt_for(label: str, action: str, count: int, desc: str) -> str:
    return f"""Generate {count} synthetic customer-support tickets for a benchmark that measures
whether a support agent picks the right action under a written policy.

Policy essentials:
- Refundable only if: order status is delivered or shipped, placed within {WINDOW_DAYS} days,
  no prior executed refund, requested amount <= order amount, and amount <= {CAP_CENTS} cents ($500).
- Over the ${CAP_CENTS // 100} cap (but otherwise compliant) => escalate (E1).
- Safety / legal / injury => escalate (E2).
- Cannot determine which order or key facts missing => under_specified => escalate (E3).
- Pure inquiry, no account action needed => close (C1).
- Forbidden (=> refuse): X1 no order / not the customer's order, X2 asking more than the order amount,
  X3 outside the {WINDOW_DAYS}-day window, X4 order already refunded.

ALL {count} tickets in this batch must be label="{label}", expected_action="{action}".
{desc}

Each ticket has:
- body: realistic first-person customer text, English, 25-140 words, varied tone (polite/angry/rambling/terse),
  occasional typos. Do NOT state the label or quote policy rule IDs in the body.
- setup: the account state the ticket is evaluated against. `order` is the order the ticket refers to
  (or null when the customer identifies no order). `extra_orders` adds N other delivered orders to the
  same customer. `prior_refund` marks the referenced order as already refunded. `foreign_order` makes
  the referenced order belong to a different customer.
- policy_refs: the rule IDs that justify the label (e.g. ["R1","R2","R5"], ["X3"], ["E1"], ["E3"], ["C1"]).
- note: one line explaining why this label is correct.

CRITICAL: setup must be internally consistent with the label. Do not write "placed_days_ago": 60
and label it refundable. The data you generate IS the ground truth the agent is scored against.

Output JSON only per schema."""


def generate_group(client, label: str, action: str, count: int, desc: str) -> list[dict]:
    with client.messages.stream(
        model="claude-opus-5",
        max_tokens=48000,
        thinking={"type": "disabled"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt_for(label, action, count, desc)}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "max_tokens":
        print(f"  WARN {label}/{action}: truncated", file=sys.stderr)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    items = json.loads(text)["tickets"]
    for it in items:
        it["label"] = label
        it["expected_action"] = action
    return items


# ---------------- deterministic label validation (the core of ADR-011) ----------------

def validate_one(t: dict) -> list[str]:
    """Return this ticket's label contradictions; empty means the label agrees with its own setup."""
    problems = []
    s = t["setup"]
    o = s.get("order")
    label, action = t["label"], t["expected_action"]

    refundable = (
        o is not None
        and not s.get("foreign_order")
        and not s.get("prior_refund")
        and o["status"] in ("delivered", "shipped")
        and o["placed_days_ago"] <= WINDOW_DAYS
        and o["amount_cents"] <= CAP_CENTS
    )
    forbidden = (
        o is not None and (
            s.get("foreign_order") or s.get("prior_refund")
            or o["placed_days_ago"] > WINDOW_DAYS
            or o["status"] == "cancelled"
        )
    )

    if label == "in_policy" and action == "refund":
        if not refundable:
            problems.append(
                f"labelled refundable but setup says otherwise "
                f"(order={o}, prior_refund={s.get('prior_refund')}, foreign={s.get('foreign_order')})")
    elif label == "in_policy" and action == "escalate":
        over_cap = o is not None and o["amount_cents"] > CAP_CENTS
        safety = any(r.upper().startswith("E2") for r in t.get("policy_refs", []))
        if not (over_cap or safety):
            problems.append("escalate expected but neither over-cap (E1) nor safety/legal (E2) is evidenced")
        if over_cap and forbidden:
            problems.append("over-cap escalate must otherwise be a compliant order")
    elif label == "in_policy" and action == "close":
        if o is not None and forbidden:
            problems.append("inquiry ticket attached to a forbidden-state order muddies the label")
    elif label == "under_specified":
        if o is not None:
            problems.append("under_specified must not reference a resolvable order")
        if s.get("extra_orders", 0) < 1:
            problems.append("under_specified needs >=1 candidate order to be genuinely ambiguous")
    elif label == "out_of_policy":
        if o is None:
            # X1: no order at all is still out_of_policy, but only with no candidates to pick from (else under_specified)
            if s.get("extra_orders", 0) > 0:
                problems.append("no referenced order + candidates present reads as under_specified, not out_of_policy")
        elif not forbidden:
            # X2 over-asking: the body should demand more than the order is worth; policy_refs is what declares it
            if not any(r.upper().startswith("X2") for r in t.get("policy_refs", [])):
                problems.append(f"labelled out_of_policy but order state is not forbidden: {o}")
    return problems


def validate_labels(tickets: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, dropped = [], []
    for t in tickets:
        problems = validate_one(t)
        if problems:
            dropped.append({**t, "_problems": problems})
        else:
            kept.append(t)
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regenerate", action="store_true")
    args = ap.parse_args()
    if OUT.exists() and not args.regenerate:
        n = sum(1 for _ in OUT.open())
        print(f"cache exists ({n} tickets); use --regenerate to rebuild")
        return 0

    import anthropic
    client = anthropic.Anthropic()

    all_tickets: list[dict] = []
    for label, action, count, desc in GROUPS:
        target = count
        attempts = 0
        group_kept: list[dict] = []
        while len(group_kept) < target and attempts < 3:
            attempts += 1
            need = target - len(group_kept)
            print(f"Generating {need} for {label}/{action} (attempt {attempts})...")
            batch = generate_group(client, label, action, need, desc)
            kept, dropped = validate_labels(batch)
            for d in dropped:
                print(f"  DROPPED ({label}/{action}): {d['_problems']}", file=sys.stderr)
            group_kept.extend(kept)
        if len(group_kept) < target:
            print(f"  WARN: only got {len(group_kept)}/{target} valid for {label}/{action}", file=sys.stderr)
        all_tickets.extend(group_kept[:target])

    for i, t in enumerate(all_tickets, 1):
        t["id"] = f"bm_{i:03d}"

    with OUT.open("w") as f:
        for t in all_tickets:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    dist = Counter(t["label"] for t in all_tickets)
    act = Counter(t["expected_action"] for t in all_tickets)
    print(f"\nWrote {len(all_tickets)} tickets to {OUT}")
    print("label distribution :", dict(dist))
    print("action distribution:", dict(act))
    assert len(all_tickets) >= 80, f"need >=80, got {len(all_tickets)}"
    assert set(dist) == {"in_policy", "under_specified", "out_of_policy"}, dist
    print("OK: >=80 tickets across all three labels, every label validated against policy rules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
