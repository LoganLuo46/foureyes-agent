/**
 * FourEyes approval console — one screen (ADR-012 / build spec §4 out of scope: no login, no chat,
 * no dashboard).
 *
 * A card has to show the operator four things before they can decide:
 *   1. What is about to happen (action + amount)
 *   2. On what grounds (agent reasoning + the evidence it actually looked up)
 *   3. Whether this ticket is suspicious (injection markers — the human sees what the agent saw)
 *   4. The customer's own words (untrusted data, labelled as such)
 */
import { useCallback, useEffect, useState } from "react";

type Approval = {
  id: string;
  ticket_id: string;
  action_type: "refund" | "escalate" | "close";
  action_payload: Record<string, unknown>;
  agent_reasoning: string;
  status: string;
  trace_url: string | null;
  created_at: string;
  subject: string;
  body: string;
  customer_name: string;
  customer_email: string;
  customer_tier: string;
};

type Detail = Approval & {
  evidence: Record<string, any>;
  injection_flags: string[];
  ticket_status: string;
};

type ExecResult = {
  approval_id: string;
  decision: string;
  execution_result: { status: string; error?: string; result?: any; verify?: any } | null;
  ticket_status: string;
};

const money = (cents: unknown) =>
  typeof cents === "number" ? `$${(cents / 100).toFixed(2)}` : "—";

const ACTION_LABEL: Record<string, string> = {
  refund: "Issue refund",
  escalate: "Escalate to specialist",
  close: "Close ticket",
};

export default function App() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [details, setDetails] = useState<Record<string, Detail>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ExecResult>>({});
  const [error, setError] = useState<string | null>(null);
  const [operator, setOperator] = useState("liming");

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/approvals?status=pending");
      if (!r.ok) throw new Error(`list failed: ${r.status}`);
      const data = await r.json();
      setApprovals(data.approvals);
      setError(null);
      // Pull each detail record — that is where injection_flags and evidence live
      const entries = await Promise.all(
        data.approvals.map(async (a: Approval) => {
          const d = await fetch(`/api/approvals/${a.id}`);
          return [a.id, await d.json()] as const;
        })
      );
      setDetails(Object.fromEntries(entries));
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  async function decide(id: string, decision: "approve" | "reject") {
    setBusy(id);
    setError(null);
    try {
      const r = await fetch(`/api/approvals/${id}/${decision}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          decided_by: operator || "console",
          reason: decision === "reject" ? "rejected in console" : null,
        }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(`${decision} failed: ${r.status} ${detail}`);
      }
      const outcome: ExecResult = await r.json();
      setResults((prev) => ({ ...prev, [id]: outcome }));
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <header>
        <div>
          <h1>FourEyes</h1>
          <p className="sub">
            Human-gated support agent — every irreversible write waits here.
          </p>
        </div>
        <div className="who">
          <label htmlFor="op">approver</label>
          <input id="op" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <span className="count">{approvals.length} pending</span>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      {approvals.length === 0 && (
        <div className="empty">
          <p>No actions awaiting approval.</p>
          <code>.venv/bin/python scripts/run_ticket.py start --category refund_eligible</code>
        </div>
      )}

      <main>
        {approvals.map((a) => {
          const d = details[a.id];
          const flags = d?.injection_flags ?? [];
          const res = results[a.id];
          return (
            <article key={a.id} className={`card ${a.action_type}`}>
              <div className="card-head">
                <span className={`badge ${a.action_type}`}>{ACTION_LABEL[a.action_type]}</span>
                {a.action_type === "refund" && (
                  <span className="amount">{money(a.action_payload.amount_cents)}</span>
                )}
                <span className="customer">
                  {a.customer_name} <em>{a.customer_tier}</em>
                </span>
              </div>

              {flags.length > 0 && (
                <div className="flags">
                  ⚠ injection markers detected in this ticket:{" "}
                  {flags.map((f) => (
                    <code key={f}>{f}</code>
                  ))}
                </div>
              )}

              <section>
                <h3>Agent reasoning</h3>
                <p className="reasoning">{a.agent_reasoning}</p>
              </section>

              <section>
                <h3>
                  Customer ticket <em>untrusted data</em>
                </h3>
                <p className="ticket-subject">{a.subject}</p>
                <p className="ticket-body">{a.body}</p>
              </section>

              {d?.evidence?.order && (
                <section>
                  <h3>Verified evidence</h3>
                  <ul className="evidence">
                    <li>
                      order {money(d.evidence.order.amount_cents)} · {d.evidence.order.status} ·
                      placed {String(d.evidence.order.placed_at).slice(0, 10)}
                    </li>
                    <li>
                      {d.evidence.customer_orders?.length ?? 0} orders on file ·{" "}
                      {d.evidence.refund_history?.length ?? 0} prior refunds
                    </li>
                  </ul>
                </section>
              )}

              {res ? (
                <div className={`result ${res.execution_result?.status}`}>
                  <strong>{res.decision}</strong> → {res.execution_result?.status ?? "no action"}
                  {res.execution_result?.error && <div className="err">{res.execution_result.error}</div>}
                  <div className="final">ticket now: {res.ticket_status}</div>
                </div>
              ) : (
                <div className="actions">
                  <button
                    className="approve"
                    disabled={busy === a.id}
                    onClick={() => decide(a.id, "approve")}
                  >
                    {busy === a.id ? "working…" : "Approve"}
                  </button>
                  <button
                    className="reject"
                    disabled={busy === a.id}
                    onClick={() => decide(a.id, "reject")}
                  >
                    Reject
                  </button>
                  {a.trace_url && (
                    <a href={a.trace_url} target="_blank" rel="noreferrer">
                      View trace →
                    </a>
                  )}
                </div>
              )}
            </article>
          );
        })}
      </main>
    </div>
  );
}
