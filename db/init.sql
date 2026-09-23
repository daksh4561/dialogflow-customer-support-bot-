-- ============================================================
-- Card Servicing Agent — Database Schema
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for digest()/hashing

-- ------------------------------------------------------------
-- CORE "CARD SYSTEM" TABLES (mocked backend of record)
-- ------------------------------------------------------------

CREATE TABLE members (
    member_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name           TEXT NOT NULL,
    email               TEXT UNIQUE NOT NULL,
    account_opened_at   TIMESTAMPTZ NOT NULL,
    credit_score_band   TEXT CHECK (credit_score_band IN ('poor','fair','good','excellent')),
    missed_payments_90d INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cards (
    card_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id           UUID NOT NULL REFERENCES members(member_id),
    card_number_masked  TEXT NOT NULL,        -- e.g. '**** **** **** 1234'
    status              TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','blocked','replacement_pending','closed')),
    credit_limit        NUMERIC(12,2) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fee_events (
    fee_event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id             UUID NOT NULL REFERENCES cards(card_id),
    fee_type            TEXT NOT NULL,        -- e.g. 'late_fee', 'annual_fee'
    amount               NUMERIC(12,2) NOT NULL,
    charged_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed             BOOLEAN NOT NULL DEFAULT FALSE,
    reversed_at          TIMESTAMPTZ
);

CREATE TABLE limit_change_requests (
    request_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id               UUID NOT NULL REFERENCES cards(card_id),
    requested_limit       NUMERIC(12,2) NOT NULL,
    previous_limit        NUMERIC(12,2) NOT NULL,
    approved               BOOLEAN,
    decided_at             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE replacement_orders (
    order_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id                UUID NOT NULL REFERENCES cards(card_id),
    reason                 TEXT NOT NULL,      -- 'lost','stolen','damaged','expiring'
    shipping_address       TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'processing'
                            CHECK (status IN ('processing','shipped','delivered','cancelled')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- CONVERSATION SESSIONS (maps Dialogflow CX sessions to members)
-- ------------------------------------------------------------

CREATE TABLE sessions (
    session_id            TEXT PRIMARY KEY,     -- Dialogflow CX session id
    member_id              UUID REFERENCES members(member_id),
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at                 TIMESTAMPTZ,
    resolution_status        TEXT DEFAULT 'in_progress'
                              CHECK (resolution_status IN ('in_progress','resolved','escalated','abandoned')),
    request_type              TEXT   -- 'fee_reversal' | 'limit_increase' | 'card_replacement' | 'other'
);

-- ------------------------------------------------------------
-- IMMUTABLE, HASH-CHAINED AUDIT LOG
-- ------------------------------------------------------------
-- Every decision/action/system-call gets one row. Each row's hash
-- depends on its own content AND the previous row's hash, so the
-- whole table forms a verifiable chain: tampering with any row
-- breaks every subsequent hash, which is detectable by replaying
-- the chain (see verify_audit_chain() below).

CREATE TABLE audit_log (
    audit_id            BIGSERIAL PRIMARY KEY,
    session_id            TEXT NOT NULL REFERENCES sessions(session_id),
    member_id              UUID REFERENCES members(member_id),
    actor                    TEXT NOT NULL CHECK (actor IN ('bot','system','human_agent','member')),
    event_type                TEXT NOT NULL,   -- 'intent_classified','eligibility_checked','action_executed','escalated','handoff', etc.
    request_type               TEXT,           -- 'fee_reversal' | 'limit_increase' | 'card_replacement' | 'other'
    decision                     TEXT,         -- e.g. 'approved','denied','escalated'
    detail                        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- free-form payload (params, confidence score, reason, etc.)
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash                        TEXT NOT NULL,      -- hash of previous row (genesis = '0'*64)
    row_hash                          TEXT NOT NULL       -- sha256(prev_hash || canonical row content)
);

-- Index for fast per-session audit retrieval (used by human-agent handoff view)
CREATE INDEX idx_audit_session ON audit_log(session_id);
CREATE INDEX idx_audit_member  ON audit_log(member_id);

-- ------------------------------------------------------------
-- HELPER: compute the next row's hash given previous hash + content
-- Call this from the application layer (FastAPI) BEFORE insert,
-- or use the trigger below to compute it automatically server-side.
-- ------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit_log_set_hash()
RETURNS TRIGGER AS $$
DECLARE
    last_hash TEXT;
    canonical TEXT;
BEGIN
    SELECT row_hash INTO last_hash
    FROM audit_log
    ORDER BY audit_id DESC
    LIMIT 1;

    IF last_hash IS NULL THEN
        last_hash := repeat('0', 64);  -- genesis hash
    END IF;

    NEW.prev_hash := last_hash;

    -- Canonical string representation of this row's immutable content
    canonical := coalesce(NEW.session_id,'') || '|' ||
                 coalesce(NEW.member_id::TEXT,'') || '|' ||
                 coalesce(NEW.actor,'') || '|' ||
                 coalesce(NEW.event_type,'') || '|' ||
                 coalesce(NEW.request_type,'') || '|' ||
                 coalesce(NEW.decision,'') || '|' ||
                 coalesce(NEW.detail::TEXT,'') || '|' ||
                 coalesce(NEW.created_at::TEXT, now()::TEXT) || '|' ||
                 last_hash;

    NEW.row_hash := encode(digest(canonical, 'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_hash
BEFORE INSERT ON audit_log
FOR EACH ROW
EXECUTE FUNCTION audit_log_set_hash();

-- Prevent UPDATE/DELETE on audit_log entirely -> true immutability
CREATE OR REPLACE FUNCTION audit_log_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_no_update
BEFORE UPDATE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation();

CREATE TRIGGER trg_audit_log_no_delete
BEFORE DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation();

-- ------------------------------------------------------------
-- VERIFICATION FUNCTION: replay the chain, return first broken link (if any)
-- Call: SELECT * FROM verify_audit_chain();
-- Empty result = chain is fully intact.
-- ------------------------------------------------------------

CREATE OR REPLACE FUNCTION verify_audit_chain()
RETURNS TABLE(audit_id BIGINT, expected_hash TEXT, actual_hash TEXT) AS $$
DECLARE
    rec RECORD;
    running_hash TEXT := repeat('0', 64);
    canonical TEXT;
    computed TEXT;
BEGIN
    FOR rec IN SELECT * FROM audit_log ORDER BY audit_id ASC LOOP
        canonical := coalesce(rec.session_id,'') || '|' ||
                     coalesce(rec.member_id::TEXT,'') || '|' ||
                     coalesce(rec.actor,'') || '|' ||
                     coalesce(rec.event_type,'') || '|' ||
                     coalesce(rec.request_type,'') || '|' ||
                     coalesce(rec.decision,'') || '|' ||
                     coalesce(rec.detail::TEXT,'') || '|' ||
                     coalesce(rec.created_at::TEXT,'') || '|' ||
                     running_hash;
        computed := encode(digest(canonical, 'sha256'), 'hex');

        IF computed != rec.row_hash OR rec.prev_hash != running_hash THEN
            audit_id := rec.audit_id;
            expected_hash := computed;
            actual_hash := rec.row_hash;
            RETURN NEXT;
        END IF;

        running_hash := rec.row_hash;
    END LOOP;
    RETURN;
END;
$$ LANGUAGE plpgsql;

-- ------------------------------------------------------------
-- SEED DATA (for demo purposes)
-- ------------------------------------------------------------

INSERT INTO members (member_id, full_name, email, account_opened_at, credit_score_band, missed_payments_90d)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'Asha Kapoor', 'asha.kapoor@example.com', now() - interval '3 years', 'good', 0),
    ('22222222-2222-2222-2222-222222222222', 'Rohit Mehta', 'rohit.mehta@example.com', now() - interval '2 months', 'fair', 1);

INSERT INTO cards (card_id, member_id, card_number_masked, status, credit_limit)
VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', '**** **** **** 4821', 'active', 150000.00),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '22222222-2222-2222-2222-222222222222', '**** **** **** 7734', 'active', 50000.00);

INSERT INTO fee_events (card_id, fee_type, amount, charged_at, reversed)
VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'late_fee', 750.00, now() - interval '10 days', FALSE);
