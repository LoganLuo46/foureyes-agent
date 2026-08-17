"""AgentState - the graph's state schema (implementation spec §6.1, plus tool_calls for trajectory assertions)."""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    ticket_id: str
    ticket_body: str            # untrusted: the customer's raw text
    sanitized_body: str         # after Layer 1 (zero-width chars stripped, content kept as evidence)
    evidence: dict              # what gather_evidence found (ticket/customer/order/orders/refunds)
    classification: str         # in_policy | under_specified | out_of_policy
    proposed_action: dict | None    # {type, payload, reasoning}
    approval_id: str | None
    approval_decision: str | None   # approved | rejected
    execution_result: dict | None
    injection_flags: list       # injection signatures Layer 1 found (kept as evidence, text not deleted)
    trace_id: str
    llm_provider: str           # which provider classify actually used (claude|gemini), for the fallback demo
    customer_reply: str         # draft reply to the customer (refusal reason / follow-up question / closing note)
    tool_calls: list            # tool names in call order, used by the trajectory assertions
    classify_result: dict       # classify's raw structured output (consumed by the propose nodes)
