"""Ticket-driver CLI — the minimal loop, plus live proof that checkpoints outlive the process.

  start  <ticket_id|--category CAT>   run to the interrupt (or END), then exit
  resume <ticket_id> approved|rejected --by NAME   brand-new process, resumed from the checkpoint
  inspect <ticket_id>                  ticket/approval/refund/audit state straight out of the DB

start and resume are separate processes: resume picking up at all is direct evidence of
"kill it → restart → approve → continue from the checkpoint instead of re-reasoning the ticket"
(S1 completion criterion 2).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import psycopg  # noqa: E402
import os  # noqa: E402


def pick_by_category(category: str) -> str:
    seed = json.loads((ROOT / "db" / "seed_data.json").read_text())
    bodies = [t["body"] for t in seed["tickets"] if t["category"] == category]
    if not bodies:
        sys.exit(f"no seed ticket in category {category}")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM tickets WHERE body = ANY(%s) AND status='open' LIMIT 1", (bodies,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"no OPEN ticket left in category {category} (rerun seed --reset?)")
        return str(row[0])


def summarize(result: dict) -> None:
    interrupted = "__interrupt__" in result
    print(f"classification : {result.get('classification')}")
    print(f"injection_flags: {result.get('injection_flags')}")
    print(f"llm_provider   : {result.get('llm_provider')}")
    print(f"tool_calls     : {result.get('tool_calls')}")
    print(f"proposed_action: {json.dumps(result.get('proposed_action'), ensure_ascii=False)}")
    print(f"approval_id    : {result.get('approval_id')}")
    print(f"interrupted    : {interrupted}")
    if interrupted:
        print(">>> Graph is parked at the approval gate (interrupt). This process now exits; "
              "use the resume subcommand to approve/reject from a new one.")
    else:
        print(f"execution      : {json.dumps(result.get('execution_result'), ensure_ascii=False)}")
        print(f"customer_reply : {result.get('customer_reply')}")


def cmd_start(args):
    from agent.graph import graph_session, start_ticket
    ticket_id = args.ticket_id or pick_by_category(args.category)
    print(f"ticket: {ticket_id}")
    with graph_session() as graph:
        result = start_ticket(graph, ticket_id)
    summarize(result)


def cmd_resume(args):
    from agent.graph import graph_session, resume_ticket
    with graph_session() as graph:
        result = resume_ticket(graph, args.ticket_id, args.decision, args.by, args.reason)
    summarize(result)


def cmd_inspect(args):
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM tickets WHERE id=%s", (args.ticket_id,))
        print("ticket status :", cur.fetchone())
        cur.execute("SELECT id, action_type, status, decided_by FROM approvals WHERE ticket_id=%s", (args.ticket_id,))
        print("approvals     :", cur.fetchall())
        cur.execute("SELECT id, amount_cents, status FROM refunds WHERE ticket_id=%s", (args.ticket_id,))
        print("refunds       :", cur.fetchall())
        cur.execute("SELECT event_type, blocked_by, created_at FROM audit_log WHERE ticket_id=%s ORDER BY created_at", (args.ticket_id,))
        for row in cur.fetchall():
            print("audit         :", row)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("ticket_id", nargs="?")
    s.add_argument("--category")
    s.set_defaults(fn=cmd_start)
    r = sub.add_parser("resume")
    r.add_argument("ticket_id")
    r.add_argument("decision", choices=["approved", "rejected"])
    r.add_argument("--by", default="cli-operator")
    r.add_argument("--reason", default=None)
    r.set_defaults(fn=cmd_resume)
    i = sub.add_parser("inspect")
    i.add_argument("ticket_id")
    i.set_defaults(fn=cmd_inspect)
    ns = ap.parse_args()
    ns.fn(ns)
