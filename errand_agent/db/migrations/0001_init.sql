-- 0001: core workflow, approval, operation journal, jobs/outbox, traces, memories, mock backend.
-- Invariants are enforced by constraints where SQL can express them; code checks are the first
-- line, constraints are the backstop.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- users and sessions
CREATE TABLE users (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    timezone    text NOT NULL DEFAULT 'Asia/Kolkata',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id          bigserial PRIMARY KEY,           -- also the advisory-lock key
    user_id     text NOT NULL REFERENCES users(id),
    state       text NOT NULL DEFAULT 'collecting' CHECK (state IN (
                    'collecting', 'comparing', 'held', 'awaiting_approval', 'executing',
                    'verifying', 'retry_wait', 'pending_verification', 'conflict',
                    'completed', 'failed')),
    revision    bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),   -- CAS counter
    request     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL
);

-- Durable per-session claim. The advisory lock serializes transitions; the claim stops two
-- workers advancing one conversation while either is outside a transaction (model/tool work).
CREATE TABLE session_claims (
    session_id        bigint PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    generation        bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    holder            text,
    lease_expires_at  timestamptz,
    claimed_at        timestamptz,
    CHECK ((holder IS NULL) = (lease_expires_at IS NULL))
);

CREATE TABLE runs (
    id                    uuid PRIMARY KEY,
    session_id            bigint NOT NULL REFERENCES sessions(id),
    claim_generation      bigint NOT NULL,
    started_at            timestamptz NOT NULL,
    deadline_at           timestamptz NOT NULL,
    ended_at              timestamptz,
    exit_reason           text,
    logical_model_calls   integer NOT NULL DEFAULT 0 CHECK (logical_model_calls >= 0),
    physical_model_calls  integer NOT NULL DEFAULT 0 CHECK (physical_model_calls >= 0),
    tool_invocations      integer NOT NULL DEFAULT 0 CHECK (tool_invocations >= 0),
    embedding_calls       integer NOT NULL DEFAULT 0 CHECK (embedding_calls >= 0),
    limits                jsonb NOT NULL,
    CHECK (logical_model_calls  <= (limits->>'logical_model_calls')::int),
    CHECK (physical_model_calls <= (limits->>'physical_model_calls')::int),
    CHECK (tool_invocations     <= (limits->>'tool_invocations')::int),
    CHECK (embedding_calls      <= (limits->>'embedding_calls')::int)
);
CREATE INDEX runs_session_idx ON runs (session_id, started_at);

-- ---------------------------------------------------------------- approval gate
-- A proposal is an immutable snapshot of one consequential action. Any material change creates a
-- new revision and supersedes the old one; approvals bind to exactly one revision.
CREATE TABLE proposals (
    id             uuid PRIMARY KEY,
    session_id     bigint NOT NULL REFERENCES sessions(id),
    revision       integer NOT NULL CHECK (revision >= 1),
    action         text NOT NULL CHECK (action IN ('book', 'cancel', 'reschedule')),
    snapshot       jsonb NOT NULL,
    snapshot_hash  text NOT NULL,
    token          text NOT NULL UNIQUE,
    status         text NOT NULL CHECK (status IN ('pending', 'approved', 'superseded', 'invalidated')),
    status_reason  text,
    supersedes     uuid REFERENCES proposals(id),
    created_at     timestamptz NOT NULL,
    UNIQUE (session_id, revision)
);
-- One actionable proposal per session.
CREATE UNIQUE INDEX proposals_one_pending ON proposals (session_id) WHERE status = 'pending';

-- Issued only when code actually rendered the card; confirmations must cite one.
CREATE TABLE display_receipts (
    id            uuid PRIMARY KEY,
    session_id    bigint NOT NULL REFERENCES sessions(id),
    proposal_id   uuid NOT NULL REFERENCES proposals(id),
    displayed_at  timestamptz NOT NULL
);
CREATE INDEX display_receipts_proposal_idx ON display_receipts (proposal_id);

CREATE TABLE approvals (
    id           uuid PRIMARY KEY,
    proposal_id  uuid NOT NULL UNIQUE REFERENCES proposals(id),     -- consumed at most once
    session_id   bigint NOT NULL REFERENCES sessions(id),
    receipt_id   uuid NOT NULL REFERENCES display_receipts(id),
    form         text NOT NULL CHECK (form IN ('yes', 'token')),
    approved_at  timestamptz NOT NULL
);

-- ---------------------------------------------------------------- operation journal
CREATE TABLE operations (
    id                    uuid PRIMARY KEY,
    session_id            bigint NOT NULL REFERENCES sessions(id),
    user_id               text NOT NULL REFERENCES users(id),
    proposal_id           uuid NOT NULL UNIQUE REFERENCES proposals(id),   -- one op per proposal
    approval_id           uuid NOT NULL UNIQUE REFERENCES approvals(id),
    action                text NOT NULL CHECK (action IN ('book', 'cancel', 'reschedule')),
    snapshot              jsonb NOT NULL,
    idempotency_key       text NOT NULL UNIQUE,
    status                text NOT NULL CHECK (status IN (
                              'approved', 'executing', 'verifying', 'retry_wait',
                              'succeeded', 'failed', 'pending_verification')),
    result_code           text,
    outcome               jsonb,
    dispatch_count        integer NOT NULL DEFAULT 0,
    reconcile_count       integer NOT NULL DEFAULT 0,
    physical_model_calls  integer NOT NULL DEFAULT 0,
    active_ms             bigint NOT NULL DEFAULT 0,
    next_retry_at         timestamptz,
    version               integer NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL,
    updated_at            timestamptz NOT NULL,
    -- Lifetime caps survive restarts and cannot be reset by a new run.
    CHECK (dispatch_count BETWEEN 0 AND 3),
    CHECK (reconcile_count BETWEEN 0 AND 6),
    CHECK (physical_model_calls BETWEEN 0 AND 40),
    CHECK (active_ms BETWEEN 0 AND 300000),
    CHECK ((status = 'retry_wait') = (next_retry_at IS NOT NULL))
);
CREATE INDEX operations_session_idx ON operations (session_id, created_at);

CREATE TABLE operation_attempts (
    id                uuid PRIMARY KEY,
    operation_id      uuid NOT NULL REFERENCES operations(id),
    kind              text NOT NULL CHECK (kind IN ('dispatch', 'reconcile')),
    ordinal           integer NOT NULL CHECK (ordinal >= 1),
    tool_name         text NOT NULL,
    run_id            uuid REFERENCES runs(id),
    claim_generation  bigint NOT NULL,
    status            text NOT NULL CHECK (status IN ('reserved', 'completed', 'failed', 'unknown', 'discarded')),
    outcome           jsonb,
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz,
    UNIQUE (operation_id, kind, ordinal)
);

-- ---------------------------------------------------------------- jobs, outbox, traces
CREATE TABLE jobs (
    id                uuid PRIMARY KEY,
    kind              text NOT NULL,
    session_id        bigint NOT NULL REFERENCES sessions(id),
    operation_id      uuid REFERENCES operations(id),
    due_at            timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'done', 'failed', 'cancelled')),
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_by        text,
    claim_expires_at  timestamptz,
    payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error        text,
    created_at        timestamptz NOT NULL
);
CREATE INDEX jobs_due_idx ON jobs (due_at) WHERE status = 'pending';
CREATE UNIQUE INDEX jobs_one_open_per_operation ON jobs (kind, operation_id)
    WHERE status IN ('pending', 'claimed') AND operation_id IS NOT NULL;

CREATE TABLE outbox (
    id            uuid PRIMARY KEY,
    user_id       text NOT NULL REFERENCES users(id),
    session_id    bigint REFERENCES sessions(id),
    event_key     text NOT NULL UNIQUE,      -- stable dedupe key
    kind          text NOT NULL,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL,
    delivered_at  timestamptz
);

CREATE TABLE trace_events (
    id            bigserial PRIMARY KEY,
    at            timestamptz NOT NULL,
    run_id        uuid,
    session_id    bigint,
    operation_id  uuid,
    kind          text NOT NULL,
    payload       jsonb NOT NULL
);
CREATE INDEX trace_events_session_idx ON trace_events (session_id, id);
CREATE INDEX trace_events_run_idx ON trace_events (run_id, id);

-- ---------------------------------------------------------------- memories (stage 5 lifecycle)
CREATE TABLE memories (
    id               uuid PRIMARY KEY,
    user_id          text NOT NULL REFERENCES users(id),
    field            text NOT NULL,
    value            jsonb NOT NULL,
    scope            text NOT NULL DEFAULT 'global',
    origin           text NOT NULL CHECK (origin IN ('explicit', 'inferred')),
    status           text NOT NULL CHECK (status IN ('active', 'unconfirmed', 'pending_embedding', 'superseded', 'forgotten')),
    confirmed        boolean NOT NULL,
    revision         integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    provenance       jsonb NOT NULL,
    embedding        vector(768),
    embedding_model  text,
    content_hash     text,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    -- Inferred facts are never active until a user confirms them.
    CHECK (NOT (origin = 'inferred' AND status = 'active' AND NOT confirmed)),
    -- Forgotten facts keep no content or vector.
    CHECK (status <> 'forgotten' OR embedding IS NULL)
);
CREATE INDEX memories_user_idx ON memories (user_id, status);

-- ---------------------------------------------------------------- mock provider backend
CREATE TABLE mock_providers (
    id              text PRIMARY KEY,
    service         text NOT NULL,
    area            text NOT NULL,
    name            text NOT NULL,
    base_price_inr  integer NOT NULL CHECK (base_price_inr > 0),
    rating          numeric(2, 1) NOT NULL CHECK (rating BETWEEN 0 AND 5),
    attributes      jsonb NOT NULL,
    schedule        jsonb NOT NULL,
    description     text NOT NULL
);
CREATE INDEX mock_providers_lookup_idx ON mock_providers (service, area);

CREATE TABLE mock_reviews (
    id           text PRIMARY KEY,
    provider_id  text NOT NULL REFERENCES mock_providers(id),
    author       text NOT NULL,
    rating       integer NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body         text NOT NULL
);

CREATE TABLE mock_holds (
    id                text PRIMARY KEY,
    user_id           text NOT NULL REFERENCES users(id),
    provider_id       text NOT NULL REFERENCES mock_providers(id),
    slot              timestamptz NOT NULL,
    quoted_price_inr  integer NOT NULL CHECK (quoted_price_inr > 0),
    created_at        timestamptz NOT NULL,
    expires_at        timestamptz NOT NULL,
    status            text NOT NULL CHECK (status IN ('active', 'booked', 'released')),
    CHECK (expires_at > created_at)
);

CREATE TABLE mock_bookings (
    id           text PRIMARY KEY,
    user_id      text NOT NULL,                    -- 'external' marks other customers
    provider_id  text NOT NULL REFERENCES mock_providers(id),
    service      text NOT NULL,
    slot         timestamptz NOT NULL,
    price_inr    integer NOT NULL CHECK (price_inr > 0),
    status       text NOT NULL CHECK (status IN ('confirmed', 'cancelled', 'cancelled_by_provider', 'completed')),
    hold_id      text UNIQUE REFERENCES mock_holds(id),   -- one booking per hold, ever
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);
-- No provider slot can be double-booked.
CREATE UNIQUE INDEX mock_bookings_slot_unique ON mock_bookings (provider_id, slot) WHERE status = 'confirmed';
CREATE INDEX mock_bookings_user_idx ON mock_bookings (user_id, slot);

-- Idempotency/replay records. A committed effect and its replay record commit together.
CREATE TABLE mock_idempotency (
    tool          text NOT NULL,
    key           text NOT NULL,
    request_hash  text NOT NULL,
    phase         text NOT NULL CHECK (phase IN ('transient_failed', 'committed', 'terminal')),
    calls         integer NOT NULL DEFAULT 1 CHECK (calls >= 1),
    result        jsonb,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    PRIMARY KEY (tool, key)
);

CREATE TABLE mock_rng (
    stream   text PRIMARY KEY,
    seed     bigint NOT NULL,
    counter  bigint NOT NULL DEFAULT 0 CHECK (counter >= 0)
);

CREATE TABLE mock_config (
    key    text PRIMARY KEY,
    value  jsonb NOT NULL
);
