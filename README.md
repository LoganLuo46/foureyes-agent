# FourEyes

**A human-gated customer-support agent.** It reads tickets, queries accounts, and decides what
to do — but **every irreversible write (refund / escalate / close) physically stops at a human
approval gate before it can execute.**

The name is the [four-eyes principle](https://en.wikipedia.org/wiki/Two-man_rule): any critical
action needs a second pair of eyes.

![Approval console](docs/assets/console.png)

---

## The argument

Most "AI agent safety" is prompt text: *"please ask a human before refunding."* A prompt is a
**request**, not a constraint — one `ignore previous instructions` and it's gone.

FourEyes puts the guarantee somewhere a prompt can't reach:

| Layer | Where it lives | What it actually does |
|---|---|---|
| ① Content | [`agent/guards.py`](agent/guards.py) | Wraps customer text in explicit untrusted-data boundaries; flags injection patterns (fake `SYSTEM:` markers, forged approvals, role hijack, base64/zero-width/homoglyph obfuscation). **Flags, never silently deletes** — the attack text is evidence. |
| ② Structure | [graph topology](agent/graph.py) + two MCP servers | The write path *physically* runs through `interrupt()`. The read-only server has no write tools — and connects as a Postgres role with **no INSERT/UPDATE/DELETE grant at all**. |
| ③ Business guardrail | [`mcp_action/guardrails.py`](mcp_action/guardrails.py) | Deterministic checks at every write tool's entry: amount ≤ order, amount ≤ $500 cap, status/window compliant, no prior refund, unique idempotency key — with a `FOR UPDATE` row lock. **Runs even when the model is fooled *and* the human mis-approves.** |

Two properties are enforced by tests, not by comments:

- **No path from START to `execute_action` bypasses `interrupt()`** — asserted by deleting the
  interrupt node from the graph and proving `execute_action` becomes unreachable.
- **Authorization comes from the approved database row, not from mutable graph state.**
  `execute_action` re-reads the `approvals` row the human signed and cross-checks it against the
  proposal; a mismatch is refused and audited. (This one came out of an adversarial review that
  found the original code *could* execute a refund while the human had approved an escalation —
  see [`failures.md`](failures.md).)

---

## Architecture

```
                                  ┌──────────────────────────────────────┐
  ticket ──▶ sanitize_input ──▶ gather_evidence ──▶ classify ──▶ route   │
             (layer ①)           (read-only MCP)     (LLM, policy)       │
                                                          │              │
              ┌───────────────────────────────────────────┤              │
              ▼                    ▼                      ▼              │
        out_of_policy       under_specified          in_policy           │
              │                    │                      │              │
        explain_refusal      propose_escalation     propose_action       │
              │                    └──────────┬───────────┘              │
             END                              ▼                          │
                                       request_approval  ── writes approvals row
                                              ▼
                                    ★ await_decision — interrupt()
                                       state → Postgres checkpoint
                                              │
                        ┌─────────────────────┴──────────────────┐
                     rejected                                 approved
                        │                                        │
                  log_rejection                            execute_action ── the ONLY
                        │                                        │            ticket-action
                       END                                verify_and_log      client
                                                                 │
                                                                END
```

Four services, one command (`docker compose up`):

| Service | Language | Role |
|---|---|---|
| `mcp-lookup` :8101 | **TypeScript** MCP SDK | Read-only tools. Connects as `foureyes_ro`. |
| `mcp-action` :8102 | **Python** MCP SDK | The only write path. Business guardrails at every tool entry. |
| `api` :8000 | FastAPI | Approval console backend. **Can only resume the graph — it has no execution capability.** |
| `postgres` :5432 | — | Business tables + LangGraph checkpoints. |

Console (`console/`, React + TypeScript + Vite) is one screen: pending cards → approve / reject.

**Why two MCP servers instead of one with two tool groups?** The permission boundary is drawn at
the *protocol and network layer*, not inside a function. Read tools are ungated because gating
everything causes [approval fatigue](docs/foureyes_build_spec.md) — a gate everywhere is a gate
nowhere. Only irreversible writes are gated.

---

## Measured results

Every number below came from a command in this repo. Nothing here is estimated.

### Adversarial testing — 53 emails, 7 attack categories

```bash
.venv/bin/python evals/test_redteam.py     # report: evals/redteam/report.json
```
```
total_emails            : 53   (direct injection · roleplay/jailbreak · forged system messages ·
                                encoding/obfuscation · social engineering · tool-parameter
                                pollution · multi-turn priming)
unauthorized_executions : 0
deception_rate          : 0.0  (0/53 talked the model into proposing a refund)
sanitize_flagged        : 21/53
blocked_by seen         : content_layer + structural_layer + business_guardrail   ← all three
```

Every email is **auto-approved** during the run — deliberately simulating a human who is *also*
fooled — so the business guardrail is the thing being tested, not the human.

The two metrics are reported separately on purpose: **zero unauthorized executions** is the
execution-layer claim; **deception rate** is the reasoning-layer experiment. Nobody wants a
refund system that is 96% safe, so the safety claim is a count, not a percentage.

### Action selection — 100 labeled tickets

```bash
.venv/bin/python evals/test_benchmark.py   # report: evals/benchmark/report.json
```
```
action_selection_accuracy : 99.0%  (99/100)
false_block_rate          : 0.0%   (0/31 actionable in_policy tickets)
per_subset                : generated 98.8% (79/80) · boundary 100% (20/20)
```

**Dataset composition matters more than the number.** 80 tickets are LLM-generated with
clean-cut policy boundaries; measuring found only 3 of them landed within ±5 days / ±$50 of a
threshold, which made 98.8% indefensible on its own. So 20 hand-written boundary cases were
added: day 30 vs day 31, exactly $500 vs $500.01, exactly the order amount vs one cent more,
`pending`/`rejected` prior refunds (which do *not* block a new refund), and three
**policy-precedence conflicts** (X3 beats E1; X4 beats E1; a safety incident outranks amount).
The boundary subset scored 20/20 — the classifier is reasoning from clauses, not keyword-matching.

The single miss (`bm_076`) cited `E3 + X1` and escalated where the label said refuse — a
third-party request made on behalf of an 84-year-old parent. Defensible disagreement, not a bug.

### Trajectory evals — 29 scenarios, and proof they have teeth

```bash
.venv/bin/python -m pytest evals/test_trajectories.py -q   # 30 passed in 124.85s
.venv/bin/python scripts/verify_eval_teeth.py
```

Trajectory evals assert the **process**, not just the answer — a correct final state can be
reached by a wrong path (the Friday-refactor that quietly routes around the approval node).
10 of the 29 are negative scenarios.

An eval suite nobody has ever seen fail is not a safety net, so failure is demonstrated on
demand: `verify_eval_teeth.py` rewrites the approval edge to `request_approval → execute_action`,
runs the evals, and requires them to go **red** — then restores the file and requires **green**:

```
=== step 1: sabotage the approval edge ===
3 failed (traj_bypass_check, traj_single_inbound_edge, traj_001), exit=1
OK: evals went RED as required
=== step 2: re-run against the intact graph ===
4 passed
VERDICT: trajectory evals have teeth
```

Note `traj_001` — a *behavioural* scenario — also goes red, not just the topology assertions.

### Test suite

```bash
.venv/bin/python -m pytest tests/ -q       # 43 passed
```
Guardrails (14) · topology (6) · consent binding (4) · layer-1 guards (16, including 6
false-positive guards so ordinary complaints stay unflagged) · guardrail backstop (3).

---

## Synthetic data disclosure

**All tickets, customers, orders and adversarial emails in this repo are LLM-generated synthetic
data.** There are no real customers, no real orders, and no production traffic. Specifically:

- `db/seed_data.json` — 60 tickets across 9 scenario categories, generated by Claude and cached
  to git so re-seeding is deterministic ([ADR-003](decisions.md)).
- `evals/redteam/emails.jsonl` — 53 adversarial emails. Six categories are Claude-generated; the
  `encoding_obfuscation` set is built programmatically (real base64 / zero-width / homoglyph
  payloads) because Claude's safety classifier declines to encode live attack instructions.
- `evals/benchmark/tickets.jsonl` — 80 labeled tickets, Claude-generated, **with every label
  checked against deterministic policy rules before entering the dataset** — one self-
  contradictory item was dropped and regenerated ([ADR-011](decisions.md)).
- `evals/benchmark/boundary.jsonl` — 20 hand-written boundary cases.

Dates are stored as relative offsets and converted at seed time, so "within the 30-day window"
scenarios stay valid whenever the dataset is re-seeded.

---

## OWASP LLM Top 10 mapping

| Risk | Where FourEyes addresses it |
|---|---|
| **LLM01 Prompt Injection** | All three layers. Content: [`agent/guards.py`](agent/guards.py) boundary-wraps and flags. Structure: injected "approval already granted" cannot skip `interrupt()`. Business: [`mcp_action/guardrails.py`](mcp_action/guardrails.py) rejects the write regardless. Measured: 53 emails, 0 unauthorized executions. |
| **LLM02 Insecure Output Handling** | Model output never reaches a tool unvalidated — `propose_action` verifies the proposed order id against fetched evidence, and guardrails re-validate every parameter at the tool boundary. |
| **LLM05 Improper Output Handling / excessive agency** | The agent cannot execute anything. `execute_action` runs only what an `approved` DB row authorizes ([ADR-009](decisions.md)). |
| **LLM06 Sensitive Information Disclosure** | Lookup server is scoped per ticket's customer; the read role has SELECT only. |
| **LLM07 System Prompt Leakage** | `prompt_extraction` is a flagged injection pattern; the policy is public by design, so leakage carries no privileged information. |
| **LLM08 Excessive Agency** | Writes gated by mandatory HITL interrupt; read/write split across two separate MCP servers with separate DB roles. |
| **LLM09 Overreliance** | Trajectory evals assert tool sequences; the benchmark measures both correctness *and* false-block rate, so over-blocking is visible rather than hidden behind a safety claim. |
| **LLM10 Model Denial of Service** | 30s timeouts, `max_retries=0` with explicit provider fallback ([ADR-006](decisions.md)). |

---

## Running it

Requires Python 3.12+, Node 20+, and Docker.

```bash
# 0. Local Python env — the scripts and evals run on the host, not in the containers
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. Full stack
cp .env.example .env          # fill in ANTHROPIC_API_KEY, GOOGLE_API_KEY, Langfuse keys
docker compose up -d --build  # postgres + mcp-lookup + mcp-action + api

# 2. Seed synthetic tickets (uses the cached generation; no API call needed)
.venv/bin/python db/seed.py --reset

# 3. Drive one ticket to the approval gate — the process then exits
.venv/bin/python scripts/run_ticket.py start --category refund_eligible

# 4. Approve from a *different* process, resuming from the Postgres checkpoint
.venv/bin/python scripts/run_ticket.py resume <ticket_id> approved --by you
.venv/bin/python scripts/run_ticket.py inspect <ticket_id>

# 5. Or approve in the console
cd console && npm install && npm run dev     # http://localhost:5173
```

Step 3 → 4 is the checkpoint demo: two separate processes. The second one resumes from the
stored checkpoint instead of re-reasoning — which matters because an LLM asked twice may reach a
*different* conclusion, and the human approved a specific proposal, not a re-roll.

---

## Design decisions

Full ADRs with alternatives-considered in [`decisions.md`](decisions.md). The load-bearing ones:

- **[ADR-002] Two DB roles.** `foureyes_ro` has no write grant, so "the lookup server is
  read-only" is a database fact rather than a code convention.
- **[ADR-007] Deterministic evidence gathering, single LLM decision point.** No ReAct tool loop —
  the trajectory assertions can be exact, and benchmark variance comes from judgment rather than
  from retrieval flakiness.
- **[ADR-007] `request_approval` and `await_decision` are separate nodes.** LangGraph replays a
  node on resume; side effects must sit *after* the `interrupt()` or the approvals row is
  written twice.
- **[ADR-009] Consent is bound to the executed action.** Authorization is re-read from the
  approved row at execution time.
- **[ADR-012] The API cannot execute.** Approving only resumes the graph, so compromising the
  console still cannot move money.

Scars, including three real bugs found after the code "worked", are in [`failures.md`](failures.md).

---

## AI-assisted development workflow

This project was built with Claude Code. What that means concretely, and how the output was
verified:

**Discipline used while building**
- Every component got a `decisions.md` entry *before* implementation — decision, alternatives,
  why, and what breaks with the alternative. Not being able to name an alternative meant the
  design wasn't understood yet.
- Every mistake went into `failures.md` with the verbatim error, the diagnosis and the fix.
- Nothing was called "done" without running it and pasting the output into the commit message.
- Output numbers (accuracy, block rates) were forbidden from appearing anywhere — code comments
  included — until a command had produced them. Placeholders read `[NOT_MEASURED]`.

**How AI output was verified**
1. **Adversarial code review.** Four independent review agents (HITL topology, injection bypass,
   guardrail completeness, correctness) produced 23 raw findings; each was then handed to a
   separate agent instructed to *refute* it against the real code. 23 → **3 confirmed**. Without
   the refutation pass the real bug would have been buried in false positives.
2. **The confirmed HIGH was a genuine design flaw**, not a typo: consent and action were
   decoupled, so a replay could have the human approve an escalation while a refund executed.
   Fixed structurally (ADR-009) plus 4 regression tests.
3. **End-to-end demos found what unit tests could not.** Layer-3 backstop and layer-1 regex gaps
   were both caught by red-team demos, after unit tests and protocol smoke tests passed — the
   bugs were in the *seams between* components.
4. **The red-team harness's own ground truth was wrong first.** It initially reported 10
   unauthorized executions; the carrier orders were accidentally *legitimately refundable*. The
   dangerous failure mode for a safety metric isn't an ugly number, it's a pretty number measured
   against the wrong baseline.
5. **Generated labels are machine-checked.** Benchmark labels are validated against deterministic
   policy rules before entering the dataset, so the metric measures agreement with the policy
   rather than agreement with another model.

---

## Out of scope (deliberately)

No voice/TTS, no chat UI, no dashboards or charts, no login system, no fine-tuning, no real user
traffic. The approval console is **one screen** — anything more is scope creep.

## Tracing

Every ticket produces one Langfuse trace, keyed deterministically off the ticket id so the
spans emitted by the *start* process and the *resume* process land in the same trace:

```
SPAN       sanitize_input        injection_flags recorded here
SPAN       gather_evidence       the five read-only lookups
GENERATION classify              policy + evidence → decision (prompt/completion/tokens)
SPAN       approval_requested    ← the graph stops here
SPAN       human_decision        ← human waited 9.7s   (waited_seconds in metadata)
SPAN       execute_action        runs only what the approved row authorises
SPAN       verify_and_log        reads the ticket back
```

`approvals.trace_url` stores the link, so every card in the console deep-links to its own trace.
Provider fallback is emitted as a `provider-fallback` event on the trace, so the Claude → Gemini
switch is visible rather than inferred.

> Region gotcha, in case you fork this: Langfuse Cloud is region-split. Pointing an
> **US** project at `cloud.langfuse.com` returns `401 Invalid credentials` — which reads like a
> bad key but isn't. Cost me a full misdiagnosis; see [`failures.md`](failures.md).

## Known gaps

- Provider fallback is verified by a **real** `APITimeoutError` (`scripts/smoke_router.py`), not
  by a mock — but it has not been exercised under an actual provider outage.
- MCP server connectivity was verified with the MCP Python SDK client (`list_tools` +
  `call_tool` over Streamable HTTP), not with the MCP Inspector UI. Protocol-equivalent, but if
  you want to claim "verified in Inspector", run it yourself first.
