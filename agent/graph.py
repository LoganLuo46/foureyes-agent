"""FourEyes LangGraph graph - safety is enforced by the topology, not by prompt wording (implementation spec §6.2).

START → sanitize_input → gather_evidence → classify
  → out_of_policy   → explain_refusal → END
  → under_specified → propose_escalation ┐
  → in_policy(esc)  → propose_escalation ├→ request_approval → await_decision(★interrupt)
  → in_policy(act)  → propose_action     ┘        → rejected → log_rejection → END
                                                  → approved → execute_action → verify_and_log → END

Topology invariants that must never be violated (asserted in tests/test_topology.py, not merely commented):
 1. The only inbound edge to execute_action is the approved branch of await_decision
 2. No path from START to execute_action bypasses await_decision (the interrupt)
 3. The ticket-action MCP URL and calls appear only inside the body of execute_action
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.guards import sanitize
from agent.mcp_client import McpToolError, lookup_call, mcp_call
from agent.prompts import CLASSIFY_SCHEMA, CLASSIFY_SYSTEM, build_classify_user
from agent.state import AgentState
from llm.router import call_llm
from llm.tracing import flush as flush_traces
from llm.tracing import observation, ticket_trace_id, trace_url

DATABASE_URL = os.environ["DATABASE_URL"]


def _db():
    return psycopg.connect(DATABASE_URL)


def _audit(cur, ticket_id, event_type, detail, blocked_by=None):
    cur.execute(
        "INSERT INTO audit_log (ticket_id, event_type, blocked_by, detail) VALUES (%s,%s,%s,%s)",
        (ticket_id, event_type, blocked_by, json.dumps(detail, default=str)))


# ---------------- nodes ----------------

def sanitize_input(state: AgentState) -> dict:
    """Layer 1: fetch the raw ticket, strip zero-width chars, flag injection patterns (evidence kept)."""
    ticket_id = state["ticket_id"]
    trace_id = ticket_trace_id(ticket_id)
    with observation("sanitize_input", trace_id=trace_id, input={"ticket_id": ticket_id}) as obs:
        t = lookup_call("get_ticket", {"ticket_id": ticket_id})
        subject = t["subject"]["untrusted_content"]
        body = t["body"]["untrusted_content"]
        raw = f"Subject: {subject}\n\n{body}"
        sanitized, flags = sanitize(raw)
        if flags:
            # Layer 1 defense in action: an instruction-like or obfuscated pattern was detected
            # and neutralized (flagged + boundary-wrapped, so downstream reads it as data rather
            # than as an instruction). This is an attempt that got stopped, so it is audited.
            with _db() as conn, conn.cursor() as cur:
                _audit(cur, ticket_id, "blocked",
                       {"node": "sanitize_input", "injection_flags": flags,
                        "note": "injection markers neutralized; treated as data, not instruction"},
                       blocked_by="content_layer")
                conn.commit()
        if obs:
            obs.update(output={"injection_flags": flags})
        return {
            "ticket_body": raw,
            "sanitized_body": sanitized,
            "injection_flags": flags,
            "trace_id": trace_id or "",
            "tool_calls": ["get_ticket"],
            "evidence": {"ticket": {k: t[k] for k in ("id", "status", "customer_id", "order_id", "created_at")}},
        }


def gather_evidence(state: AgentState) -> dict:
    """Deterministic evidence gathering (ADR-007): a fixed sequence of lookups, read-only, no gate."""
    ev = dict(state["evidence"])
    calls = list(state["tool_calls"])
    tk = ev["ticket"]
    with observation("gather_evidence", trace_id=state.get("trace_id") or None) as obs:
        ev["customer"] = lookup_call("get_customer", {"customer_id": tk["customer_id"]})
        calls.append("get_customer")
        if tk.get("order_id"):
            ev["order"] = lookup_call("get_order", {"order_id": tk["order_id"]})
            calls.append("get_order")
        else:
            ev["order"] = None
        ev["customer_orders"] = lookup_call(
            "list_customer_orders", {"customer_id": tk["customer_id"], "limit": 10})
        calls.append("list_customer_orders")
        ev["refund_history"] = lookup_call("get_refund_history", {"customer_id": tk["customer_id"]})
        calls.append("get_refund_history")
        if obs:
            obs.update(output={"orders": len(ev["customer_orders"]), "refunds": len(ev["refund_history"])})
    return {"evidence": ev, "tool_calls": calls}


def classify(state: AgentState) -> dict:
    """The only LLM decision point: full policy text + trusted evidence + flagged untrusted body -> a 3-way label."""
    result = call_llm(
        system=CLASSIFY_SYSTEM,
        user=build_classify_user(state["evidence"], state["sanitized_body"], state["injection_flags"]),
        json_schema=CLASSIFY_SCHEMA,
        trace_name="classify",
        trace_id=state.get("trace_id") or None,
    )
    parsed = result.parsed
    return {
        "classification": parsed["classification"],
        "classify_result": parsed,
        "customer_reply": parsed.get("customer_reply", ""),
        "llm_provider": result.provider,
    }


def route_after_classify(state: AgentState) -> str:
    c = state["classification"]
    action = state["classify_result"].get("action")
    if c == "out_of_policy":
        return "refuse"
    if c == "under_specified":
        return "escalate"
    # in_policy: route on the action; incoherent combos (e.g. in_policy+refuse) escalate, to stay safe
    if action == "refund" or action == "close":
        return "act"
    return "escalate"


def explain_refusal(state: AgentState) -> dict:
    """out_of_policy: refuse and say why. No account writes at all; the ticket stays open for a human."""
    with _db() as conn, conn.cursor() as cur:
        _audit(cur, state["ticket_id"], "tool_call",
               {"node": "explain_refusal", "policy_refs": state["classify_result"].get("policy_refs"),
                "reply": state.get("customer_reply", "")})
        conn.commit()
    return {"proposed_action": None}


def propose_action(state: AgentState) -> dict:
    """Builds the refund/close proposal for in_policy. A refund missing parameters degrades to escalation."""
    cr = state["classify_result"]
    ticket_id = state["ticket_id"]
    if cr["action"] == "refund":
        order_id = cr.get("refund_order_id")
        amount = cr.get("refund_amount_cents")
        # Whitelist = the customer's 10 most recent orders plus the one order this ticket directly
        # references (which may fall outside those 10). The referenced order must belong to this
        # ticket's customer, otherwise it is left out - that keeps the X1 ownership check intact.
        ev = state["evidence"]
        known_orders = {o["id"] for o in ev.get("customer_orders", [])}
        ref_order = ev.get("order")
        ticket_customer = ev.get("ticket", {}).get("customer_id")
        if ref_order and ref_order.get("customer_id") == ticket_customer:
            known_orders.add(ref_order["id"])
        if not order_id or order_id not in known_orders or not isinstance(amount, int) or amount <= 0:
            return {"proposed_action": {
                "type": "escalate",
                "payload": {"reason": "classifier proposed refund with invalid/unverified parameters",
                            "severity": "medium"},
                "reasoning": cr.get("reasoning", "")}}
        return {"proposed_action": {
            "type": "refund",
            "payload": {"order_id": order_id, "amount_cents": amount,
                        "reason": cr.get("reasoning", "")[:500],
                        "idempotency_key": f"refund-{ticket_id}-{order_id}"},
            "reasoning": cr.get("reasoning", "")}}
    return {"proposed_action": {
        "type": "close",
        "payload": {"resolution_note": state.get("customer_reply") or "resolved: informational inquiry"},
        "reasoning": cr.get("reasoning", "")}}


def propose_escalation(state: AgentState) -> dict:
    cr = state["classify_result"]
    severity = cr.get("escalation_severity")
    if severity not in ("low", "medium", "high"):
        severity = "medium"
    reason = (cr.get("reasoning") or "requires human review")[:500]
    return {"proposed_action": {
        "type": "escalate",
        "payload": {"reason": reason, "severity": severity},
        "reasoning": cr.get("reasoning", "")}}


def _action_fingerprint(action_type: str, payload: dict) -> str:
    """Canonicalize (action type, payload) into a comparable fingerprint, binding what a human approved to what will run."""
    return json.dumps({"type": action_type, "payload": payload}, sort_keys=True, default=str)


def request_approval(state: AgentState) -> dict:
    """Write the approvals row and mark the ticket pending_approval.

    Idempotent **and bound to the action**: a pending row is reused only when its **fingerprint
    matches** the current proposal. If a pending row exists but the action has changed (say a
    replay where classify flipped from escalate to refund), the stale row is marked rejected
    (system:superseded) and a fresh row is inserted. A human must never approve A and have the
    system execute B.
    """
    ticket_id = state["ticket_id"]
    pa = state["proposed_action"]
    fp = _action_fingerprint(pa["type"], pa["payload"])
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, action_type, action_payload FROM approvals "
            "WHERE ticket_id=%s AND status='pending'", (ticket_id,))
        for aid, atype, apayload in cur.fetchall():
            if _action_fingerprint(atype, apayload) == fp:
                return {"approval_id": str(aid)}
            # Stale pending row (the action has changed): void it, never reuse it
            cur.execute(
                "UPDATE approvals SET status='rejected', decided_at=%s, decided_by=%s WHERE id=%s",
                (datetime.now(timezone.utc), "system:superseded", aid))
            _audit(cur, ticket_id, "rejected",
                   {"approval_id": str(aid), "decided_by": "system:superseded",
                    "reason": "proposal changed before human decision; stale approval voided"})
        cur.execute(
            "INSERT INTO approvals (ticket_id, thread_id, action_type, action_payload, "
            "agent_reasoning, evidence, status, trace_url) VALUES (%s,%s,%s,%s,%s,%s,'pending',%s) "
            "RETURNING id",
            (ticket_id, ticket_id, pa["type"], json.dumps(pa["payload"], default=str),
             pa.get("reasoning", ""), json.dumps(state["evidence"], default=str),
             trace_url(state.get("trace_id") or None)))
        approval_id = str(cur.fetchone()[0])
        with observation("approval_requested", trace_id=state.get("trace_id") or None,
                         input={"action": pa["type"], "payload": pa["payload"]},
                         metadata={"approval_id": approval_id,
                                   "injection_flags": state.get("injection_flags", [])}):
            pass  # marks where the approval gate sits in the trace (the demo points at it: the graph stops here)
        cur.execute("UPDATE tickets SET status='pending_approval' WHERE id=%s", (ticket_id,))
        _audit(cur, ticket_id, "approval_requested",
               {"approval_id": approval_id, "action": pa["type"], "payload": pa["payload"],
                "injection_flags": state.get("injection_flags", [])})
        # Layer 2 defense in action: even when the ticket smuggles in a bypass-the-approval
        # injection (forged approval / role hijack / tool direction), the topology still forces
        # this write through the human gate. The bypass fails structurally, and it is audited.
        bypass = {"fake_approval_claim", "role_hijack", "tool_direction", "direct_override"}
        hit = sorted(bypass & set(state.get("injection_flags", [])))
        if hit:
            _audit(cur, ticket_id, "blocked",
                   {"node": "request_approval", "bypass_flags": hit,
                    "note": "injection attempted to skip approval; write still gated by mandatory HITL interrupt"},
                   blocked_by="structural_layer")
        conn.commit()
    return {"approval_id": approval_id}


def await_decision(state: AgentState) -> dict:
    """★ The graph pauses here (interrupt) and the state lands in the checkpoint. A human approve/reject resumes it.

    Side effects live strictly **after** the interrupt: when resume replays this node, interrupt
    returns the human's decision immediately, so the code below it runs exactly once (ADR-007).
    """
    decision = interrupt({
        "approval_id": state.get("approval_id"),
        "ticket_id": state["ticket_id"],
        "action": state["proposed_action"],
        "injection_flags": state.get("injection_flags", []),
    })
    verdict = (decision or {}).get("decision")
    if verdict not in ("approved", "rejected"):
        raise ValueError(f"resume payload must carry decision approved|rejected, got {decision!r}")
    decided_by = (decision or {}).get("decided_by", "unknown")
    now = datetime.now(timezone.utc)
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT created_at FROM approvals WHERE id=%s", (state["approval_id"],))
        row = cur.fetchone()
        waited_s = (now - row[0]).total_seconds() if row else None
        cur.execute(
            "UPDATE approvals SET status=%s, decided_at=%s, decided_by=%s WHERE id=%s",
            (verdict, now, decided_by, state["approval_id"]))
        _audit(cur, state["ticket_id"], verdict,
               {"approval_id": state["approval_id"], "decided_by": decided_by,
                "reason": (decision or {}).get("reason"), "waited_seconds": waited_s})
        conn.commit()
    # Human-decision span, carrying the **wait duration** (spec §11). That gap is the approval
    # pause, and it is the entire reason a checkpoint exists: it can run for hours, outliving the process.
    with observation("human_decision", trace_id=state.get("trace_id") or None,
                     input={"approval_id": state["approval_id"]},
                     output={"decision": verdict, "decided_by": decided_by},
                     metadata={"waited_seconds": waited_s,
                               "reason": (decision or {}).get("reason")}):
        pass
    return {"approval_decision": verdict}


def route_after_decision(state: AgentState) -> str:
    return "approved" if state["approval_decision"] == "approved" else "rejected"


def log_rejection(state: AgentState) -> dict:
    """Human rejected: ticket goes back to open, nothing is executed. (await_decision already wrote the audit row.)"""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tickets SET status='open' WHERE id=%s", (state["ticket_id"],))
        conn.commit()
    return {"execution_result": {"status": "rejected_by_human"}}


def execute_action(state: AgentState) -> dict:
    """The only node that ever calls ticket-action (topology invariant 3).

    Authority comes from **the row the human approved** (approvals), not from mutable graph state:
    re-read the approval row with status='approved' from the DB, execute the action **it** records,
    and cross-check that against state.proposed_action - any mismatch aborts (structural_layer block).
    That makes "the human approved A but the system executed B" structurally impossible; this is where
    the four-eyes binding actually lives. Even if a human approves the wrong thing, mcp_action's
    Layer 3 guardrail still reviews it independently -> status == "blocked".
    """
    # The ticket-action MCP URL is allowed to appear only inside this function body (tests/test_topology.py asserts it via AST)
    action_url = os.environ.get("MCP_ACTION_URL", "http://localhost:8102/mcp")
    ticket_id = state["ticket_id"]
    approval_id = state.get("approval_id")
    calls = list(state["tool_calls"])

    # Source of truth for authorization: the approved approvals row
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT action_type, action_payload, status FROM approvals WHERE id=%s",
                    (approval_id,))
        row = cur.fetchone()
    if row is None or row[2] != "approved":
        outcome = {"status": "blocked",
                   "error": f"no approved approval row for id={approval_id} (status={row[2] if row else None})"}
        with _db() as conn, conn.cursor() as cur:
            _audit(cur, ticket_id, "blocked", {"node": "execute_action", "reason": outcome["error"]},
                   blocked_by="structural_layer")
            conn.commit()
        return {"execution_result": outcome, "tool_calls": calls}
    approved_type, approved_payload = row[0], row[1]

    # Cross-check: the approved action must match the graph's current proposal, otherwise abort (run neither)
    pa = state["proposed_action"]
    if _action_fingerprint(approved_type, approved_payload) != _action_fingerprint(pa["type"], pa["payload"]):
        outcome = {"status": "blocked",
                   "error": "approved action does not match proposed action; refusing to execute either",
                   "approved": {"type": approved_type, "payload": approved_payload},
                   "proposed": {"type": pa["type"], "payload": pa["payload"]}}
        with _db() as conn, conn.cursor() as cur:
            _audit(cur, ticket_id, "blocked",
                   {"node": "execute_action", "reason": outcome["error"],
                    "approved": outcome["approved"], "proposed": outcome["proposed"]},
                   blocked_by="structural_layer")
            conn.commit()
        return {"execution_result": outcome, "tool_calls": calls}

    # What runs is the action recorded on the approved row (the authoritative source), not graph state
    pa = {"type": approved_type, "payload": approved_payload}
    tool = {"refund": "issue_refund", "escalate": "escalate_ticket", "close": "close_ticket"}[pa["type"]]
    args = {"ticket_id": ticket_id, **pa["payload"]}
    with observation("execute_action", trace_id=state.get("trace_id") or None,
                     input={"tool": tool, "args": args}) as obs:
        try:
            result = mcp_call(action_url, tool, args)
            calls.append(tool)
            outcome = {"status": "executed", "result": result}
        except McpToolError as e:
            calls.append(tool)
            outcome = {"status": "blocked", "error": str(e)}
        if obs:
            obs.update(output=outcome)
    return {"execution_result": outcome, "tool_calls": calls}


def verify_and_log(state: AgentState) -> dict:
    """Read back after execution: does the ticket's final status match the action? On a guardrail block, return it to open."""
    ticket_id = state["ticket_id"]
    outcome = dict(state["execution_result"])
    expected = {"refund": "resolved", "escalate": "escalated", "close": "resolved"}[
        state["proposed_action"]["type"]]
    if outcome["status"] == "blocked":
        # Approved but stopped by Layer 3: ticket returns to open for a human (the action server audited the block)
        with _db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE tickets SET status='open' WHERE id=%s", (ticket_id,))
            conn.commit()
        outcome["verify"] = {"expected_status": expected, "actual_status": "open",
                             "consistent": False, "note": "blocked by business guardrail after approval"}
        return {"execution_result": outcome}
    with observation("verify_and_log", trace_id=state.get("trace_id") or None) as obs:
        t = lookup_call("get_ticket", {"ticket_id": ticket_id})
        actual = t["status"]
        outcome["verify"] = {"expected_status": expected, "actual_status": actual,
                             "consistent": actual == expected}
        if obs:
            obs.update(output=outcome["verify"])
    with _db() as conn, conn.cursor() as cur:
        _audit(cur, ticket_id, "tool_call",
               {"node": "verify_and_log", "verify": outcome["verify"]})
        conn.commit()
    # The read-back is a real tool call too, so it belongs in the trajectory (assert the process, not just the result)
    return {"execution_result": outcome, "tool_calls": [*state["tool_calls"], "get_ticket"]}


# ---------------- graph ----------------

def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("sanitize_input", sanitize_input)
    g.add_node("gather_evidence", gather_evidence)
    g.add_node("classify", classify)
    g.add_node("explain_refusal", explain_refusal)
    g.add_node("propose_action", propose_action)
    g.add_node("propose_escalation", propose_escalation)
    g.add_node("request_approval", request_approval)
    g.add_node("await_decision", await_decision)
    g.add_node("log_rejection", log_rejection)
    g.add_node("execute_action", execute_action)
    g.add_node("verify_and_log", verify_and_log)

    g.add_edge(START, "sanitize_input")
    g.add_edge("sanitize_input", "gather_evidence")
    g.add_edge("gather_evidence", "classify")
    g.add_conditional_edges("classify", route_after_classify, {
        "refuse": "explain_refusal",
        "escalate": "propose_escalation",
        "act": "propose_action",
    })
    g.add_edge("explain_refusal", END)
    g.add_edge("propose_action", "request_approval")
    g.add_edge("propose_escalation", "request_approval")
    g.add_edge("request_approval", "await_decision")
    g.add_conditional_edges("await_decision", route_after_decision, {
        "approved": "execute_action",   # the one and only edge into execute_action
        "rejected": "log_rejection",
    })
    g.add_edge("log_rejection", END)
    g.add_edge("execute_action", "verify_and_log")
    g.add_edge("verify_and_log", END)
    return g.compile(checkpointer=checkpointer)


@contextmanager
def graph_session():
    """Graph session backed by a PostgresSaver checkpointer. thread_id = ticket_id.

    Traces are flushed on exit: Langfuse exports in batches, and a short-lived process like
    `run_ticket.py resume` exits the instant it finishes, so execute_action's span never gets
    sent. We lost spans exactly this way.
    """
    with PostgresSaver.from_conn_string(DATABASE_URL) as saver:
        saver.setup()
        try:
            yield build_graph(saver)
        finally:
            flush_traces()


def thread_config(ticket_id: str) -> dict:
    return {"configurable": {"thread_id": ticket_id}}


def start_ticket(graph, ticket_id: str) -> dict:
    """Run until the interrupt (or END). Returns the state snapshot at the pause, or at the end."""
    return graph.invoke({"ticket_id": ticket_id}, config=thread_config(ticket_id))


def resume_ticket(graph, ticket_id: str, decision: str, decided_by: str, reason: str | None = None) -> dict:
    """Resume execution from the checkpoint once a human has decided."""
    return graph.invoke(
        Command(resume={"decision": decision, "decided_by": decided_by, "reason": reason}),
        config=thread_config(ticket_id))
