# failures.md — things that broke

> One entry per screw-up: what I tried / how it broke (verbatim error) / how I fixed it
> (or the two ways out). Worth more than a log of what worked. Write it the same day.

---

## 2026-08-11 · the mcp Python SDK 2.0 renamed the client API

**What I tried:** Wrote the connectivity test the 1.x way — `from mcp.client.streamable_http import streamablehttp_client`
— assuming the transport yields the triple `(read, write, get_session_id)` and the result carries `r.isError`.

**How it broke:**
`ImportError: cannot import name 'streamablehttp_client' ... Did you mean: 'streamable_http_client'?`
Fixed that, then it blew up on `AttributeError: 'CallToolResult' object has no attribute 'isError'. Did you mean: 'is_error'?`

**How I fixed it:** mcp 2.0.0 went snake_case across the board: `streamable_http_client` (what it yields
is a `TransportStreams`, so unpack with `(read, write, *_)` to stay compatible) and `CallToolResult.is_error`.
Lesson: on a major SDK bump, run `inspect.signature` before writing anything — don't trust muscle memory. The TS SDK (1.12) doesn't have this problem.

---

## 2026-08-11 · blocked successfully, audit lost: attack input blew up audit_log's FK

**What I tried:** After the guardrail rejected a nonexistent ticket_id, write the block record to `audit_log(ticket_id=...)`.

**How it broke:** The MCP protocol smoke test came back with an error string that wasn't the guardrail message, but
`insert or update on table "audit_log" violates foreign key constraint` —
audit_log.ticket_id has an FK to tickets, so **a fabricated ticket_id makes the audit write itself fail**.
The caller sees a rejection (is_error), but nothing is left behind in audit_log. The red-team report is generated
from that table, so an attacker only has to cite a fake ticket number to leave no trace. A malformed uuid is worse: SQL throws
InvalidTextRepresentation directly, before the guardrail path is even reached.

**How I fixed it:** `_blocked()` now verifies ticket existence first: if it doesn't exist (or is malformed), the fabricated id
moves into `detail.claimed_ticket_id` and the audit row's ticket_id lands NULL — the audit always persists.
Added uuid format validation at all three tool entry points, so malformed input goes down the guardrail rejection path.
Two new regression tests pin it down. Lesson: **the audit path is itself an attack surface**, and the "logging failed" failure mode
is sneakier than "blocking failed" — only the protocol-level smoke test (against a real server) exposed it; the unit tests
(direct function calls) missed it, because the unit tests all used valid uuids.

---

## 2026-08-11 · Gemini 2.5 is retired for new keys; Langfuse key invalid (⚠️ needs a human)

**What I tried:** Point the router's fallback path at `gemini-2.5-flash`; ship traces to Langfuse with the key in .env.

**How it broke:**
1. Gemini: `404 NOT_FOUND ... models/gemini-2.5-flash is no longer available to new users`
   — it's still in models.list, but generateContent is disabled for new users. "In the list" ≠ "callable".
2. Langfuse: `401 Unauthorized`. Tried both hosts, EU (cloud.langfuse.com) and US (us.cloud.langfuse.com),
   both 401 → **the key itself is invalid**, not a region misconfiguration.

**How I fixed it:**
1. Used models.list to find a currently available model; fallback is now `gemini-3.6-flash`
   (latest stable flash, response_json_schema verified working), with GEMINI_MODEL overridable via env.
2. Skipped Langfuse under the "two attempts and it still doesn't work" rule: tracing.py runs auth_check in get_tracer
   and degrades the whole thing to a no-op on failure (otherwise the background export thread spams 401s). **No system
   functionality is affected**, but every trace-related acceptance item (Langfuse replay, switch point visible,
   trace_url in approvals) is outstanding. **First thing in the morning: regenerate pk/sk in the Langfuse project
   settings, put them in .env, then rerun `scripts/smoke_router.py` and the demo to fill in the traces. No code changes needed.**

---

## 2026-08-11 · adversarial review found a structural hole in four-eyes: consent decoupled from action

**What I tried:** Ran a multi-perspective adversarial review workflow over the core code (4-perspective finder → adversarial verification of each finding).

**How it broke (HIGH, confirmed):** `request_approval` reuses any pending approval row keyed only on `ticket_id`,
while `execute_action` executes `state['proposed_action']` **from graph state** and never reads back
"the row the human actually approved". Attack/replay path: the same thread (= ticket_id) is rerun,
classify flips from escalate to refund, the new proposal is checkpointed as refund, but request_approval
picks up the old pending escalate row and reuses its id; the approval console renders an escalate card, the human approves
"escalate", and after resume execute_action executes `state.proposed_action` = refund.
**The human signs for A, the system does B — the refund moves money while the consent record says escalate.**
The layer ③ guardrail can't save this either: a refund that is itself compliant sails through cleanly — the guardrail has no idea *what the human approved*.
This is exactly the thing this project exists to prove impossible (the four-eyes principle). The review also confirmed 2 minor bugs:
propose_action's allowlist misses the order the ticket directly references (with >10 orders on a customer, a legitimate refund gets wrongly downgraded to an escalation);
escalate with an invalid severity throws before the DB connection is opened, so the block never reaches the audit log.

**How I fixed it:**
- **The source of truth for authorization is now the DB row, not graph state.** execute_action reads back the
  `status='approved'` row in `approvals` and executes the action_type/action_payload recorded on **it**, cross-checking
  against state.proposed_action — on mismatch, blocked + logged as structural_layer, and neither one runs.
- request_approval only reuses a pending row whose **fingerprint matches** the current proposal; if the action changed, the old row is
  marked `rejected(system:superseded)` and a new row is inserted.
- propose_action's allowlist now folds in `evidence.order` (ownership verified); the escalate severity check
  moved inside the connection so it goes through _blocked and reaches the audit log.
- Added `tests/test_consent_binding.py`, 4 tests nailing it down: escalate approved but state says refund →
  execution refused + structural_layer audit + zero refunds; an unapproved row never drives execution; matching pairs execute normally;
  stale pending rows get voided instead of reused.

## 2026-08-11 · the red-team harness had the wrong ground truth (a metric that fooled me)

**What I tried:** Attach 53 red-team emails to "carrier orders", run the full graph, count unauthorized executions.

**How it broke:** The first run produced `unauthorized_executions: 10`, which looked like the system had been breached.
Looking closer, **my harness was wrong**: the `inquiry` carrier I'd built was an order that was delivered, within 30 days, never refunded
— which under policy **should be refunded**. So classify correctly said in_policy, the guardrail correctly let it through,
a legitimate refund executed, and my script labeled it "unauthorized". **The metric was wrong in its ground truth, not the system.**

**How I fixed it:** Rebuilt every carrier order to be **strictly non-refundable** under policy (past the 30-day window X3 / already has an executed
refund X4 / belongs to someone else X1). That way any refund that executes is necessarily unauthorized, and the measurement is clean.
Rerun: 53 emails, 0 unauthorized.

**Lesson:** The most dangerous failure mode for a security metric isn't "the number looks bad", it's **the number looks good but measures the wrong thing**.
Had I gone the other way — carriers that absolutely cannot be refunded, but with injections that never got a chance to propose a refund at all —
I'd have walked away with a fake, beautiful 0. The red-team harness itself has to be auditable: *why* each carrier is non-refundable belongs in a comment, hardcoded.

---

## 2026-08-11 · ⚠️ Anthropic credits exhausted — stopping work per the rules

**Verbatim error:** `400 invalid_request_error: Your credit balance is too low to access the
Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.`
(request_id: req_011CdwG7bfNxnH9a8bt89q9t), hit at classify during the second red-team run.

**Status:** This is one of the [must stop] conditions. Building stopped; PROGRESS.md written.
The router classifies 400 as a content error and rethrows (ADR-006's design), so **it did not fall back to Gemini** —
which is correct: a billing problem should not be silently absorbed by Gemini, or every evaluation number quietly
becomes a Gemini number while the résumé says "Claude primary + Gemini fallback". **Gemini-side quota is verified still working**
(`gemini-3.6-flash` returns OK), so if I want to keep going before topping up, I can temporarily point `CLAUDE_MODEL` at
Gemini — but **any benchmark number produced that way must be labeled as Gemini output** and must not be mixed into conclusions about Claude.

---

**Lesson:** This is the single most valuable fix in the project, and the core ammunition for the interview —
"the approval record *is* the authorization; execution reads from the approved row, not from mutable model state".
Adversarial verification (dispatch an independent skeptic per finding to falsify it + actually read the code) is what separated it
from the noise in the 22 raw findings: 23 raw → 3 confirmed. A finder alone would have drowned it in false positives.

---

## 2026-08-11 · the graph crashed on a guardrail block (McpToolError swallowed by ExceptionGroup)

**What I tried:** Added the "human approves by mistake + guardrail backstop" end-to-end demo (TOCTOU double refund). It blew up on the first run.

**How it broke:** The guardrail blocked correctly (`BLOCKED[business_guardrail] ... already has an executed
refund (policy X4 / R3)`), but the exception went straight through `execute_action`'s `except McpToolError`
and the whole graph crashed. Cause: `mcp_call` uses `asyncio.run()` internally, and anyio's TaskGroup wraps
`McpToolError` inside a `BaseExceptionGroup`, which `except McpToolError` naturally can't catch.

**How I fixed it:** `mcp_call` now catches `BaseExceptionGroup` at the outer layer, **recursively unwraps** the McpToolError,
and rethrows it (`agent/mcp_client.py::_unwrap`). Added `tests/test_guardrail_backstop.py` with
3 tests: a guardrail block returns blocked instead of raising, the ticket goes back to open after a block, and the unwrap logic itself.

**Lesson:** This path had **never been tested end to end** — unit tests called the guardrail function directly (no MCP),
and the protocol smoke test only looked at `is_error` (no graph). All three segments were tested individually, and the assembly was broken.
**Only an end-to-end demo finds the seams between layers.** It also shows the red-team demo isn't just attack validation —
it's a form of integration test.

---

## 2026-08-11 · the red-team demo turned around and caught a detection gap in layer ①

**What I tried:** `demo_structural_gate` hits the graph with a normal question plus a forged mid-sentence `SYSTEM: already approved`.

**How it broke:** `injection_flags` came back empty — the attack wasn't flagged at all. Checked the regexes:
1. `fake_system_message` requires `(^|\n)` at line start, but the attack reads `...with the hub? SYSTEM: ...`,
   buried mid-sentence → missed.
2. `fake_approval_claim` hardcodes `has\s+been\s+`, and the attack says `has already been granted`,
   with an adverb wedged in → missed.
3. `no human review is required` wasn't covered at all (only `no approval required` was recognized).

**How I fixed it:** Loosened all three regexes: the role marker may follow sentence-ending punctuation; 0-3 words may sit
between has/was/is and approved/granted; added a `(no|without) ... (approval|review|sign-off)
... (required|needed|necessary)` branch. Created `tests/test_guards.py` with 16 tests that pin down
those three regressions **and add a false-positive line of defense** (6 pieces of normal customer text must produce zero flags,
including sentences like "The system keeps disconnecting" that contain "system" but no role marker).
After the fix, the sanitize flag rate went 15/53 → 21/53.

**One thing worth noting on the positive side:** even with layer ① failing to flag it, **the topology still forced this write to stop at the approval gate**
(`interrupted=True` in the demo). That is what defense in depth is for — layer ① is an **evidence layer**,
not a blocking layer; what actually stops the write is the layer ② topology. The red-team report has to make that division of labor clear,
or "missed flag" gets misread as "breached".

---

## 2026-08-11 · I misdiagnosed a wrong region as an invalid key (an SDK singleton hid the evidence)

**What I tried:** Langfuse was returning 401, so I wrote a loop that used the SDK to try the EU and US hosts in turn:

```python
for host in (os.environ.get("LANGFUSE_HOST"), "https://us.cloud.langfuse.com"):
    lf = Langfuse(host=host, public_key=..., secret_key=...)
    print(host, "auth_check:", lf.auth_check())
```

Both came back 401 → I concluded "the key itself is invalid, not a region problem" and handed that off in PROGRESS as a blocker.

**How it broke:** **The conclusion was wrong.** Testing them separately with curl:

```
cloud.langfuse.com     → 401 {"message":"Invalid credentials. Confirm that you've configured the correct host."}
us.cloud.langfuse.com  → 200 {"data":[{"name":"My Project","organization":{"name":"Liming's Organization"}}]}
```

The key was fine the whole time; the project lives in the **US region** and `.env` pointed at EU. The reason the second
iteration also 401'd is that **the Langfuse SDK keeps a client singleton**: after the first `Langfuse(host=EU)`,
the second construction with the US host was ignored and it was still hitting EU. My "I tried both hosts" was a lie.

**How I fixed it:** Changed the host in `.env` to `https://us.cloud.langfuse.com`; auth_check went True immediately.
Added a comment to `.env.example` marking it region-sensitive, plus what that 401 message actually means.

**Lesson (more important than the fix):**
1. **Diagnose vendor problems with the dumbest tool available.** curl is a clean process every time; an SDK may hold
   singletons, connection pools, cached global config — using it for an A/B comparison gives you false negatives.
2. The error message literally said `Confirm that you've configured the correct host`,
   but I saw the exception type `UnauthorizedError` and attributed from there — **I never read the response body**.
   Same class of mistake as the mcp_action one: the answer was inside the error object and I didn't open it.
3. Writing "key is invalid, please regenerate" in a handoff doc sends whoever picks it up to do **exactly the wrong thing**
   (a fresh key still 401s). A misdiagnosis is worse than no diagnosis.

---

## 2026-08-11 · filling in the trace: the approval pause wasn't on the trace at all

**What I tried:** With the host fixed, verify trace completeness against the span list in spec §11.

**How it broke:** There were only 4 spans (sanitize / gather / classify / execute); the three the spec requires —
**approval requested, human decision (with wait time), verify** — were all missing.
Which means the trace doesn't show "the graph stopped here and waited for a person" — the exact thing this whole project exists to demonstrate.
On top of that, the resume process exits immediately after executing, so Langfuse's batch export never gets sent (the first query showed 3 spans,
and only after 30 seconds did it become 4) — live in the demo it would look like data was lost.

**How I fixed it:**
- `request_approval` emits an `approval_requested` span (carrying injection_flags);
- `await_decision` emits a `human_decision` span after interrupt returns, with
  **waited_seconds** in metadata (from approvals.created_at to the moment of decision);
- `verify_and_log` emits a span;
- `graph_session()` calls `flush()` on exit so short-lived processes land their spans deterministically.

Measured: a complete trace is now 7 observations, and `human_decision` shows
`← human waited 9.7s`. Continuity across processes holds: the trace created by the start process is the same one the resume process's
execute_action attaches to (trace_id is derived deterministically from ticket_id).

**Lesson:** "the trace is wired up" and "the trace tells a story" are two different things. The first is getting the SDK working;
the second requires every **narrative node** to have a corresponding span — with no span at the approval pause,
all you can do in the demo is point at a blank stretch and say "it waited three hours here".
