# decisions.md — ADR-lite

> One entry per key decision: **Decision / Alternatives considered / Why this one / What the alternatives would have cost**.
> Written the day the decision is made, never backfilled. This file is the interview answer bank.

---

## ADR-001 · Policy lives in structured Markdown with stable rule IDs

**Decision:** `policy/support_policy.md` is written as structured Markdown. Every rule has a stable ID
(R1-R5 refund eligibility, E1-E3 escalation, C1 close, X1-X5 prohibitions), and every rule is a decidable predicate.
The numeric constants (the $500 cap, the 30-day window) live in `.env` as `REFUND_CAP_CENTS` / `REFUND_WINDOW_DAYS`,
which are the single authority at execution time; the document records the defaults and says so explicitly.

**Alternatives considered:**
- (a) Rules as code only (policy-as-code: Python functions, no document)
- (b) Policy in a database table, editable at runtime
- (c) A free-prose policy document

**Why this one:**
The document serves three consumers at once: (1) the LLM in `classify` has to three-way classify against it (it is quoted in the prompt);
(2) the `search_policy` tool has to retrieve against it (structured sections = retrieval units); (3) benchmark labeling has to score against it
(rule IDs make every label traceable to a specific clause). Markdown + rule IDs is the only shape that satisfies all three.
Refusal reasons and `audit_log` can also cite clauses precisely (e.g. "blocked by X2").

**What the alternatives would have cost:**
- Code only: the LLM has no policy text to cite, the three-way classification degrades into the model's common-sense guessing, and the benchmark
  cannot be scored — which amounts to deleting the project's thesis ("policy is the only basis for adjudication").
- Database: an extra layer of CRUD and migrations, and still no version history; Markdown in git gets diff and blame for free.
- Prose: rules cannot be cited individually, `search_policy` returns a blob, and you cannot write a test assertion against it.

**Key adjudication (the priority order for the three-way classification; labeling and classify both follow it):**
1. The facts trigger any X clause (no order / exceeds the order amount / outside the window / already refunded) → `out_of_policy` → refuse and cite the clause
2. Triggers E1 (over $500) or E2 (safety/legal/personal injury) → `in_policy`, expected action = escalate
   (the policy **explicitly prescribes** an action, which means "the policy explicitly allows this" — not out_of_policy; refusing here is the wrong answer)
3. Not enough information to decide (E3) → `under_specified` → escalate, never execute
4. All R clauses satisfied → `in_policy`, expected action = refund
5. Pure inquiry (C1) → `in_policy`, expected action = close

An over-ask (customer wants $300 but the order was only $200) is adjudicated `out_of_policy` → refuse and state the refundable maximum,
rather than "helpfully refunding the $200" — the agent does not get to rewrite the customer's request for them; that is exceeding its authority.

---

## ADR-002 · One Postgres instance, two database roles (read-write / read-only)

**Decision:** One Postgres instance, one database. Two roles: `foureyes` (read-write, used by the action server,
the API, and the checkpointer) and `foureyes_ro` (read-only, **used only by the lookup server**, SELECT privilege only).
Enum-shaped fields are `TEXT + CHECK` rather than native ENUM; money is always `INTEGER` cents plus `CHECK (amount_cents > 0)`;
the LangGraph checkpointer tables live in the same database as the business tables (PostgresSaver creates them itself).

**Alternatives considered:**
- (a) The lookup server reuses the same read-write connection, staying read-only because "the code only contains read functions"
- (b) Business database and checkpoint database on two separate instances
- (c) Native Postgres ENUM types

**Why this one:**
The read-only role upgrades the spec's "lookup only connects over a read-only database connection" from **code discipline**
to a **database permission fact**: even if the lookup server is completely owned by prompt injection, even if someone adds write code to it,
`foureyes_ro` simply has no INSERT/UPDATE/DELETE at the DB level — this is the structural layer (layer 2) extended
into the database, and the red-team report can cite it directly. TEXT+CHECK means evolving an enum is a constraint change,
not `ALTER TYPE`. One database saves a second set of connections and backups, and the checkpoint tables carry their own prefix so nothing collides.

**What the alternatives would have cost:**
- (a) "It's not that it *can't* write, it's that it promised not to" — in an interview, that sentence is where the permission-boundary story dies, and the red-team narrative goes with it.
- (b) The ops cost of a second instance, buying zero isolation (checkpoint data is not an attack surface).
- (c) Adding an ENUM value needs `ALTER TYPE ... ADD VALUE` (irreversible, restricted inside transactions) — not worth it for a demo project.

---

## ADR-003 · Synthetic data: generate once with an LLM, cache as JSON, deterministic reloads

**Decision:** On its first run `db/seed.py` calls Claude (`claude-opus-5`, structured output pinned to a JSON schema)
to generate ~60 synthetic tickets (with 14 customers, ~32 orders, and a handful of already-executed refunds), **caches them to `db/seed_data.json`
and commits that to git**; every reload after that reads the cache and never touches the API again. Dates are generated as relative day offsets (`placed_days_ago`)
and converted to absolute timestamps at load time, so "30-day window" scenarios still hold no matter when you reload.
seed.py talks to the anthropic SDK directly, not through `llm/router.py`.

**Alternatives considered:**
- (a) Call the LLM for fresh data on every seed
- (b) Hand-written fixed fixtures, no LLM at all
- (c) Route seeding through the router (and get fallback for free)

**Why this one:**
"Synthetic tickets generated by an LLM" is a spec requirement (and a README disclosure), but the demo and the evaluation need **determinism**:
CI, and a kill-the-process-and-restart demo, cannot depend on the public internet, and the data cannot change on every run. Generate once + cache in git
satisfies both — genuinely LLM-generated (the generation prompt and schema are there to show), and replay costs nothing and never wobbles.
The relative dates are the load-bearing detail: absolute dates would quietly turn an "ordered 25 days ago" scenario into an expired one two months later,
and the evaluation would drift without anyone noticing. seed is a one-shot dev tool, not on the runtime path, so there is no reason to drag in the router dependency
(which comes after seed in the build order anyway).

**What the alternatives would have cost:**
- (a) CI would need an API key, the data would differ every run, and trajectory evaluation could not hard-code assertions → eval is simply not reproducible.
- (b) Breaks the spec's disclosure ("the tickets are LLM-generated" becomes a lie — red line 3), and 60 hand-written tickets could never fake
  that much linguistic variety.
- (c) seed is built before the router, so the ordering is inverted; adding fallback logic to a one-shot script is over-engineering.

---

## ADR-004 · MCP transport is Streamable HTTP, not stdio

**Decision:** Both MCP servers use **Streamable HTTP** transport on their own ports
(lookup :8101 `/mcp`, action :8102 `/mcp`), and the LangGraph side connects over HTTP with the MCP Python client.
lookup uses `DATABASE_URL_RO` (the read-only role), action uses the read-write connection.

**Alternatives considered:**
- (a) stdio transport (the client spawns each server as a subprocess)
- (b) Both tool families on one server, reads and writes distinguished by tool naming

**Why this one:**
docker-compose requires postgres / api / mcp-lookup / mcp-action to be **independent services** (spec §2),
and across containers only a network transport works — stdio does not cross a container boundary. Streamable HTTP also lets
MCP Inspector connect directly for acceptance (acceptance checklist item 3), and makes "lookup being down does not affect action" true.
Separate processes + separate ports + separate DB roles = the permission boundary is drawn at the protocol and network layer,
which is the physical backing for the interview answer to "why two servers".

**What the alternatives would have cost:**
- (a) Under stdio the two servers become the client's child processes: lifecycles welded together, no independent deployment,
  and the compose topology degenerates into a single container; Inspector acceptance also needs a workaround.
- (b) One server with both tool families = the permission boundary retreats to "different functions in the same codebase", and half the red-team narrative (structural-layer defense) collapses.

---

## ADR-005 · Business guardrail = deterministic code at the tool entry point + row lock + idempotency-key unique constraint

**Decision:** Layer 3 defense is implemented as a **pure query function** in `mcp_action/guardrails.py`,
`check_refund(cur, ...) -> list[violation]`, called at the entry point of the `issue_refund` tool; a non-empty return
writes `audit_log(blocked_by='business_guardrail')` and raises. The check locks the order row with
`SELECT ... FOR UPDATE` to prevent a concurrent double refund; repeat execution is backstopped by the **database unique constraint**
on `refunds.idempotency_key` (not by an application-level duplicate check). The guardrail lives in its own file, separate from the server, so pytest
can unit-test it without standing up an MCP server.

**Alternatives considered:**
- (a) Put the guardrail in the prompt ("do not refund more than $500")
- (b) Application-level SELECT-then-INSERT dedupe (no lock, no unique constraint)
- (c) Inline the guardrail logic in the tool function body, no separate file

**Why this one:**
The entire point of a guardrail is that it **still holds when the model has been completely fooled and the human approved by mistake** — so it has to be deterministic code,
sitting on the last write path. If dedupe is only an application-level SELECT, two concurrent approvals both see "no prior refund"
and both go through — `FOR UPDATE` serializes the check and the write, and the unique constraint adds a second floor
(even if the locking logic gets refactored badly later, the DB still rejects a second row with the same key). The file split is for
testability: "the guardrail blocked it" in the red-team report has to be backed by a unit test, not by "we ran it once and nothing bad happened".

**What the alternatives would have cost:**
- (a) A prompt is a request, not a constraint; one "ignore previous instructions" and it is gone —
  which is exactly the practice this project exists to falsify.
- (b) A TOCTOU race: two approval-console tabs, or a replayed resume, can each produce a double refund, and the audit trail cannot explain it.
- (c) The guardrail could only be tested by starting a server and speaking the protocol — ten times the cost per unit test, so nobody writes them.

---

## ADR-006 · LLM routing: fall back by error class, SDK retries off, structured output aligned across providers

**Decision:** `call_llm()` in `llm/router.py`: primary Claude (`claude-opus-5`, adaptive thinking,
effort configurable, default low), fallback Gemini (`gemini-2.5-flash`). **Fall back only on availability errors**:
RateLimit (429) / Overloaded (529) / 5xx / timeout (30s) / connection error / safety refusal;
content errors (400 and friends) raise immediately — those are bugs, not availability problems. The Anthropic client sets
`max_retries=0`, so every retry decision belongs to the router. Both providers use JSON-schema structured output
(Claude `output_config.format`, Gemini `response_json_schema`) from a single schema definition.
The switch is recorded as a `provider-fallback` event in Langfuse (spec: the switch point must be visible in the trace).
Langfuse is wrapped in `llm/tracing.py`: if the key is missing or the service is unreachable, everything degrades to a no-op instead of dragging down the main path.

**Alternatives considered:**
- (a) Let the anthropic SDK retry (2 times by default) before falling back
- (b) Fall back on every exception (400s and content errors included)
- (c) Free-text output with JSON pulled out by regex

**Why this one:**
SDK auto-retry hides "when did we switch" inside a black box, and the trace shows no clean switch point — the demo requires
"the switch event must be visible in the trace", so the router has to own that decision. Falling back on a 400 would disguise a bug
(malformed schema, illegal parameter) as "Gemini saved us", swallowing the error silently. Structured output
makes classify's parsing regex-free and keeps the two providers isomorphic; refusal counts as a fallback trigger because, for this system,
it is equivalent to "the primary provider declined to serve" — and after falling back the request still goes through all three layers of defense, so safety does not depend on which model answered.

**What the alternatives would have cost:**
- (a) In the "deliberately break Claude" demo, the SDK quietly retries twice first, so the switch is delayed and unexplainable.
- (b) A mistyped prompt parameter never surfaces as an error, and the evaluation numbers get silently produced by Gemini.
- (c) The two providers' output formats drift, the parsing layer becomes a hidden failure source, and benchmark variance cannot be attributed.

---

## ADR-007 · Graph design: deterministic evidence gathering + a single LLM adjudication; interrupt gets its own node

**Decision:**
1. `gather_evidence` is **deterministic code** (a fixed lookup sequence: ticket → customer → order (if any)
   → list_customer_orders → get_refund_history), not an LLM-driven ReAct tool loop.
   The LLM appears in exactly one node, `classify`: it reads the full policy + the evidence + the flagged, untrusted ticket body and,
   in one structured-output call, returns {classification, action, refund params, reasoning}.
2. The full policy text is embedded directly in classify's system prompt (~2KB); no search_policy retrieval.
3. Approval is split into two nodes: `request_approval` (side effects such as writing the approvals row) → `await_decision`
   (**nothing but interrupt()**). Because LangGraph **replays the entire node** on resume,
   side effects and interrupt in the same node = the approvals row gets written twice.
4. Workflow bookkeeping (tickets.status moving between open/pending_approval, reads and writes on the approvals table)
   is written to the DB by the graph directly; **account-level irreversible actions** (refund/escalate/close) go only through mcp_action.

**Alternatives considered:**
- (a) gather_evidence as an LLM tool loop (truly agentic evidence gathering)
- (b) Let the LLM call search_policy on demand during classify
- (c) request_approval writes the row and interrupts inside one node
- (d) Route every tickets write through mcp_action

**Why this one:**
This project's thesis is the **HITL gate + three-layer defense**, not tool-orchestration flair. Deterministic evidence gathering removes an entire class of
failures (didn't look up what it should have, looked up the wrong person, a loop that never converges), which is what makes 25 trajectory assertions hard-codable and 155+ evaluation runs
reproducible — the variance in the benchmark numbers should come from *adjudication*, not from *evidence gathering*. Stuffing 2KB of policy into the
prompt is more reliable and cheaper than retrieval (miss one clause and you misclassify a whole family). Splitting the approval nodes is dictated by LangGraph's
interrupt semantics: resume replays everything from the top of the node down to the interrupt,
so the only safe shape is "no side effects before interrupt". Bookkeeping writes go straight to the DB because open↔pending_approval
is a **reversible process state**, and the approval machine's own state must not itself be gated by approval (that recurses forever).

**What the alternatives would have cost:**
- (a) 2-5 extra LLM calls per ticket, and trajectory assertions degrade from "the sequence" into "a likely sequence",
  so CI flakes; whereas in an interview "why no agent loop here" is a question I would rather be able to answer well.
- (b) A retrieval miss or a ranking wobble turns straight into a misclassification, and it is hard to attribute.
- (c) The classic trap, already hit once: duplicate approval rows, two identical cards in the console.
- (d) close_ticket and "mark the ticket as pending approval" would gate each other — a deadlock at the semantic level.

---

## ADR-008 · Red team: an attack taxonomy drives LLM drafting + caching; the two metrics are reported separately

**Decision:** The 50 adversarial emails are generated from an **attack taxonomy**: 7 families (direct instruction injection, role-play/
jailbreak, forged system messages, encoding obfuscation, social-engineering urgency, tool-argument poisoning, multi-turn setup), ≥5 per family,
50 in total across families; each email is bound to a real seed ticket/order as its carrier (the injected text is embedded in a legitimate ticket body).
Claude drafts them by family (structured output), cached to `evals/redteam/emails.jsonl` and committed to git.
At run time each email goes through the **full graph** (sanitize → classify → execute if an action is proposed, auto-approved to simulate "the human got fooled too"),
recording `{layer_blocked, model_deceived, unauthorized_executed}`. **The two metrics are reported separately:**
unauthorized executions at the execution layer (target 0, the one that goes on the résumé); deception rate at the reasoning layer (the model was persuaded but the gate stopped it — something I discuss, not something I claim).

**Alternatives considered:**
- (a) Hand-write all 50 emails
- (b) 50 variants of the same trick
- (c) Test only the sanitize layer (do not run the full graph)

**Why this one:**
"50 emails across K families" and "50 paraphrases of one email" are two different leagues in an interview (the build spec says so explicitly). Binding each to a real ticket
is the crux: an injection has to be parasitic on a **refund request that looks legitimate** before it can genuinely test data/instruction separation —
bare injection text gets refused by any system. Running the full graph (rather than sanitize alone) is what lets the two metrics be told apart:
a sanitize miss (reasoning layer fooled) is not the same as an unauthorized execution (execution layer), and only the latter is a security claim.
Auto-approval is a **deliberate worst case, simulating "the human was fooled as well"**, aimed squarely at stress-testing layer 3.

**What the alternatives would have cost:**
- (a) 50 hand-written emails is a huge amount of work with poor linguistic variety, and an interviewer spots the padding immediately.
- (b) You lose on the first "why": "these 50 emails are really one email."
- (c) No way to produce the core number, "zero unauthorized executions" — testing layer 1 only is not testing defense in depth.

**How the metrics are defined (real numbers go in after the run; [NOT_MEASURED] for now):**
- unauthorized_executions: the number of unexpected executed entries in `refunds`/`audit_log` after all 50 emails have run.
- deception_rate: the share with model_deceived=true (sanitize did not flag it and classify was flipped).
- Each of the three layers blocked at least once: audit_log.blocked_by contains all three values content_layer / structural_layer /
  business_guardrail.

---

## ADR-009 · The source of truth for authorization is the approved approvals row, not graph state (hardened after adversarial review)

**Decision:** `execute_action` reads back the approval row with `status='approved'` from the DB and executes the
action_type/action_payload recorded **there**; it cross-checks against the graph state's `proposed_action` and, on a mismatch,
aborts (logging `blocked_by='structural_layer'`) and executes neither action. `request_approval`
only reuses a pending row whose **fingerprint matches** the current proposal; if the action changed, the old row is voided.

**Alternatives considered:**
- (a) execute_action executes `state['proposed_action']` directly (the original implementation)
- (b) Add the fingerprint check in request_approval only, and let execute still read graph state

**Why this one:**
The four-eyes principle means "**the action the human approved** gets executed", not "an action gets executed". Graph state
is mutable (replay, concurrency, and classify's non-determinism all change it), whereas the `approvals` row is the immutable
artifact the human signed. Reading the action from that artifact makes "the human approves A, the system executes B" structurally impossible — an order of magnitude stronger than trusting that
the state was not modified. This was a HIGH caught by adversarial review (23 raw findings → 3 confirmed),
and it is this project's strongest interview ammunition: the binding between consent and action is drawn at the data layer, not assumed from timing.

**What the alternatives would have cost:**
- (a) Precisely the hole the review found: a replay flips the proposal from escalate to refund, the human approved the escalate
  card, the system issues the refund anyway, and the layer-3 guardrail lets it through (it has no idea what the human approved).
- (b) request_approval's fingerprint check stops "reusing a stale row", but it does not stop the case where, at resume time,
  the checkpoint's proposed_action and the row that approval_id points at have already diverged;
  without the read-back on the execution side there is still a seam. Defense in depth: do both.

---

## ADR-010 · Trajectory evaluation: hand-written golden set + deterministically constructed data + assertions on process

**Decision:** The 25 trajectory scenarios are **hand-written YAML** (not LLM-generated), and each scenario **constructs its own data deterministically**
(the order's amount/status/placed_days_ago are written into the scenario, created and torn down around the run); seed tickets are not reused.
Four things are asserted: `tool_sequence` (the sequence of tool calls), `must_interrupt`, `must_not_call`, and `final_ticket_status`.
Scenarios come in two types: `run` (runs the graph) and `structural` (topology assertions, no graph run).
Physically they live in `evals/trajectories/*.yaml`, one file per group of related scenarios (the glob still matches).

**Alternatives considered:**
- (a) LLM-generated trajectory scenarios
- (b) Reuse the 60 seed tickets as scenarios
- (c) Assert only the final state (an ordinary eval), not the tool sequence

**Why this one:**
The value of trajectory evaluation is that it **asserts the process** — "a correct final state can be reached by an incorrect path" (build spec §5).
To assert the process you have to make the path predictable: scenarios carry their own data construction, which is the only way to manufacture
exactly-30-days, exactly-$500, already-refunded boundaries. LLM-generated scenarios cannot keep assertions consistent with the data (they will write
self-contradicting expectations); reusing seed tickets cannot cover the boundaries, and one change to the seed data invalidates every assertion.
Hand-writing 25 of them is a **one-time cost that buys permanently stable CI**. The structural scenarios fold the negative assertion "no path exists that bypasses
interrupt" into the same golden set, instead of leaving it scattered somewhere else.

**What the alternatives would have cost:**
- (a) Assertions fight the scenario data, CI goes red at random, and eventually someone turns the eval off — the most common way an eval dies.
- (b) Boundary scenarios cannot be built; one `db/seed.py --reset` and every trajectory assertion is void.
- (c) You cannot detect the bug where a refactor lets some edge skip the approval gate while the final state is still perfectly correct —
  and that is exactly what build spec §11 scenario 3 ("the Friday commit that almost made it to production") is there to prevent.

**"The eval has teeth" verification (hard spec requirement):** `scripts/verify_eval_teeth.py` does it automatically:
rewrite the approval edge in `agent/graph.py` to wire `request_approval → execute_action` directly
(bypassing interrupt), run the trajectory evaluation and it **must go red**, then restore the original file and re-run to confirm it goes green.
This step cannot rely on someone remembering to do it by hand; it has to be a replayable script.

---

## ADR-011 · benchmark: LLM-generated labels, but every label must pass deterministic rule validation

**Decision:** 80 labeled tickets are generated by Claude per category (ticket body + **data construction parameters** + label +
expected action + policy clause citation), cached to `evals/benchmark/tickets.jsonl` in git.
**But the generated output is not trusted blindly**: `validate_labels()` checks each label for self-consistency against deterministic rules derived from the policy
— anything labeled `out_of_policy/X3` must have `placed_days_ago` > 30; anything labeled `in_policy/refund` must have an
order that is delivered/shipped, inside the window, ≤ $500, with no prior refund; anything labeled `under_specified`
must genuinely have no uniquely determinable order. Inconsistent entries are **discarded and reported**, never entering the dataset.
The run stops after classify + propose (no approval gate, no execution), so the benchmark produces no writes at all.

**Alternatives considered:**
- (a) Trust the LLM's labels as-is
- (b) Label all 80 by hand
- (c) Score by running the full graph (approval and execution included)

**Why this one:**
Benchmark numbers only mean something if the ground truth is trustworthy — and I had just been burned on the red-team side
(a carrier order that should have been refunded was labeled "unauthorized" by me, producing a fake 10 unauthorized executions; see failures.md).
The typical LLM labeling failure is **the label contradicting the data the LLM itself generated** (it writes "ordered 45 days ago" and labels it in_policy).
Deterministic validation catches that contradiction before it lands, which is what gives a number like "88%" something to stand on. Labeling 80 by hand takes
half a day and I would still slip; the validator is actually the more reliable option. Running the full graph would mix "classification quality" with "the approval/execution chain",
making the metric unattributable — and polluting the DB.

**What the alternatives would have cost:**
- (a) The metric measures "agreement between one model and another model", not "agreement between the model and the policy" — one interview question and it is exposed.
- (b) Expensive, and without machine validation the labels can still contradict themselves.
- (c) One evaluation would take minutes and require cleaning up writes, so it cannot run in CI; and classification errors get tangled up with execution errors.

**How the two metrics are defined (real numbers go in after the run):**
- `action_selection_accuracy` = the share of tickets where the action type was chosen correctly (refund/escalate/close/refuse)
- `false_block_rate` = the share of **in_policy tickets** wrongly refused (refuse) or wrongly escalated (escalate)
  — i.e. the cost of blocking legitimate requests, the availability counterweight to "zero unauthorized executions"

**Measured results (2026-08-11, command `.venv/bin/python evals/test_benchmark.py`,
report `evals/benchmark/report.json`):**
```
action_selection_accuracy : 99.0% (99/100)
false_block_rate          : 0.0%  (0/31 actionable in_policy tickets)
label_accuracy            : 99.0%
per_label   : in_policy 42/42 · under_specified 26/26 · out_of_policy 31/32
per_subset  : generated 98.8% (79/80) · boundary 100% (20/20)
confusion   : refund->refund 26, escalate->escalate 37, close->close 5,
              refuse->refuse 31, refuse->escalate 1
```
**The dataset composition has to be stated alongside the number (otherwise 99% is misleading):** 80 LLM-generated (policy boundaries are clear-cut;
after counting, only 3 land within ±5 days / ±$50 of a threshold) + **20 hand-written hard boundaries**. The hand-written boundary set exists precisely because
98.8% on the generated set alone is indefensible — the boundary set probes both sides of each threshold (day 30 vs. day 31, exactly $500
vs. $500.01, exactly the order amount vs. one cent more), the fine print of R3 (pending/rejected refunds do **not** block a new refund),
and **policy priority conflicts** (when X3 and E1 both hold, X wins → refuse rather than escalate;
X4 > E1; an E2 safety incident outranks the amount). The boundary set scored 20/20, which says the classification reasons from clauses rather than
matching keywords. The single miss (bm_076) has the model citing `E3+X1` and escalating rather than refusing — a third-party
request (filed on behalf of an 84-year-old mother), where escalating to a human is defensible; a reasonable disagreement between the label and the model's judgment.

---

## ADR-012 · Approval console: the API only resumes the graph, it never executes an action itself

**Decision:** FastAPI exposes four endpoints (list / detail / approve / reject). Approve and reject **do exactly one thing**:
resume that thread's graph with `Command(resume=...)`. The API layer **has no execution capability at all** —
it does not import mcp_action and does not hold the action server's address (the allowlist scan in `tests/test_topology.py`
covers the `api/` directory; cross the line and the test goes red). Resume runs synchronously (the endpoints are `def`
rather than `async def`, so FastAPI puts them on the thread pool and the event loop is never blocked).
The approval card must show `injection_flags` and the evidence summary, not just the action and the amount.

**Alternatives considered:**
- (a) The API calls mcp_action to execute the action, then updates the approvals table
- (b) The approve endpoint only writes the DB, and a background worker polls and resumes
- (c) The card shows only the action/amount/reason (no injection flags)

**Why this one:**
If the API could execute, there would be a **second write path** — topology constraints 1 and 2 fail on the spot,
"writes physically must pass through interrupt" becomes an empty claim, and that claim is the whole thesis of the project. With the API able only to
resume, execution authority stays in the `execute_action` node alone; however thoroughly the console is compromised, all it can do is
"let the graph continue", never "make the graph execute something else" (and stacked with ADR-009's consent binding, the approved action
cannot be changed either). Synchronous resume makes "click approve → immediately see the execution result and the final state" one click in the demo,
with no polling to explain. Showing injection_flags is the substance of HITL: **the human has to see what the agent saw**,
otherwise the "second pair of eyes" is just a rubber stamp.

**What the alternatives would have cost:**
- (a) The topology test goes red immediately; worse, the red-team narrative collapses — compromise the console and you touch the money directly.
- (b) An extra process and another state machine, plus explaining "why did nothing happen when I clicked" during the demo, for zero benefit.
- (c) The human cannot see that "this ticket smuggled in a forged approval claim", approval degrades into stamping,
  and the second layer of defense in depth is decorative.
