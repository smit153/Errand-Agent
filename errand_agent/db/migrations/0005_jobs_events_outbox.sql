-- 0005: background work (stage 6; DECISIONS §§8, 9, 11; v2 §4).
--
-- * Jobs: a fixed set of kinds; provider-cancellation jobs reference an ingested event and may
--   not have a session yet. One open job per (kind, operation) and per (kind, event).
-- * Operations remember when the model's proposal passed the snapshot check, so a worker can
--   finish an approved, validated dispatch after a crash without asking the model again, and the
--   request constraints the user approved under (used to search for a replacement later).
-- * Provider events: the mock provider's outgoing feed (simulation side) and our inbox, where a
--   unique key per source event and per (booking, kind) makes ingestion idempotent.
-- * Outbox: delivery bookkeeping for at-least-once delivery with stable notification IDs.

ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('operation_execute', 'operation_retry', 'operation_reconcile', 'provider_cancellation'));
ALTER TABLE jobs ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE jobs ADD COLUMN event_id uuid;
ALTER TABLE jobs ADD COLUMN finished_at timestamptz;
ALTER TABLE jobs ADD CONSTRAINT jobs_claimed_has_owner
    CHECK (status <> 'claimed' OR (claimed_by IS NOT NULL AND claim_expires_at IS NOT NULL));
CREATE INDEX jobs_claim_expiry_idx ON jobs (claim_expires_at) WHERE status = 'claimed';

ALTER TABLE operations ADD COLUMN proposal_checked_at timestamptz;
ALTER TABLE proposals ADD COLUMN constraints jsonb;
ALTER TABLE operations ADD COLUMN constraints jsonb;

-- The mock provider's outgoing event feed. Written only by the simulation control; duplicates of
-- the same cancellation (different event IDs) are allowed here on purpose.
CREATE TABLE mock_provider_events (
    id          text PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('booking_cancelled')),
    booking_id  text NOT NULL,
    user_id     text NOT NULL,
    emitted_at  timestamptz NOT NULL
);

-- Our inbox of provider events: ingestion is idempotent per source event and per booking.
CREATE TABLE provider_events (
    id                   uuid PRIMARY KEY,
    source_event_id      text NOT NULL UNIQUE,
    kind                 text NOT NULL CHECK (kind IN ('booking_cancelled')),
    booking_id           text NOT NULL,
    user_id              text NOT NULL REFERENCES users(id),
    received_at          timestamptz NOT NULL,
    status               text NOT NULL DEFAULT 'received'
                             CHECK (status IN ('received', 'handled', 'ignored')),
    followup_session_id  bigint REFERENCES sessions(id),
    outcome              jsonb,
    UNIQUE (booking_id, kind)
);
ALTER TABLE jobs ADD CONSTRAINT jobs_event_fk FOREIGN KEY (event_id) REFERENCES provider_events(id);
CREATE UNIQUE INDEX jobs_one_open_per_event ON jobs (kind, event_id)
    WHERE status IN ('pending', 'claimed') AND event_id IS NOT NULL;

ALTER TABLE outbox ADD COLUMN fetch_count integer NOT NULL DEFAULT 0 CHECK (fetch_count >= 0);
ALTER TABLE outbox ADD COLUMN last_fetched_at timestamptz;
CREATE INDEX outbox_undelivered_idx ON outbox (user_id, created_at) WHERE delivered_at IS NULL;
