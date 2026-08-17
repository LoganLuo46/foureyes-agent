-- FourEyes schema · contract in docs/foureyes_implementation_spec.md §3
-- Hard requirements: amounts are always integer cents; refunds.idempotency_key is unique; audit_log records blocked attempts.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS customers (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email       TEXT NOT NULL,
  name        TEXT NOT NULL,
  tier        TEXT NOT NULL CHECK (tier IN ('free', 'pro', 'enterprise')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   UUID NOT NULL REFERENCES customers(id),
  amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),  -- cents, never float
  currency      TEXT NOT NULL DEFAULT 'USD',
  status        TEXT NOT NULL CHECK (status IN ('paid', 'shipped', 'delivered', 'cancelled')),
  placed_at     TIMESTAMPTZ NOT NULL,
  delivered_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tickets (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  UUID NOT NULL REFERENCES customers(id),
  order_id     UUID REFERENCES orders(id),
  subject      TEXT NOT NULL,
  body         TEXT NOT NULL,  -- ★ untrusted data: the customer's own words, stored verbatim
  status       TEXT NOT NULL CHECK (status IN ('open', 'pending_approval', 'resolved', 'escalated')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id        UUID NOT NULL REFERENCES tickets(id),
  order_id         UUID NOT NULL REFERENCES orders(id),
  amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
  status           TEXT NOT NULL CHECK (status IN ('pending', 'executed', 'rejected')),
  idempotency_key  TEXT UNIQUE NOT NULL,  -- ★ stops double execution
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  executed_at      TIMESTAMPTZ
);

-- ★ core table behind the approval console
CREATE TABLE IF NOT EXISTS approvals (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id        UUID NOT NULL REFERENCES tickets(id),
  thread_id        TEXT NOT NULL,  -- LangGraph checkpoint thread id
  action_type      TEXT NOT NULL CHECK (action_type IN ('refund', 'escalate', 'close')),
  action_payload   JSONB NOT NULL,
  agent_reasoning  TEXT NOT NULL,
  evidence         JSONB NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  trace_url        TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at       TIMESTAMPTZ,
  decided_by       TEXT
);

-- Audit trail of every action, including blocked attempts (the red-team report comes straight out of this table)
CREATE TABLE IF NOT EXISTS audit_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id   UUID REFERENCES tickets(id),
  event_type  TEXT NOT NULL CHECK (event_type IN (
                'tool_call', 'approval_requested', 'approved',
                'rejected', 'executed', 'blocked')),
  blocked_by  TEXT CHECK (blocked_by IN ('content_layer', 'structural_layer', 'business_guardrail')),
  detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer    ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_customer   ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order      ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status   ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_audit_ticket       ON audit_log(ticket_id);

-- ★ Read-only role, for the lookup server only. The structural-layer defense extended into
--   the DB (ADR-002): even if the lookup code is compromised, this role cannot write at all.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'foureyes_ro') THEN
    CREATE ROLE foureyes_ro LOGIN PASSWORD 'foureyes_ro';
  END IF;
END $$;

GRANT CONNECT ON DATABASE foureyes TO foureyes_ro;
GRANT USAGE ON SCHEMA public TO foureyes_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO foureyes_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO foureyes_ro;
-- Explicitly revoke every write privilege (guards against future GRANT drift)
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM foureyes_ro;
