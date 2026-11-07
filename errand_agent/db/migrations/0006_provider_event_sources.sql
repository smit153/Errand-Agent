-- 0006: every source event ID maps to the one canonical provider event for its booking, so
-- duplicate deliveries of the same cancellation (different source IDs) are recorded once and
-- never re-read by ingestion (E44).
CREATE TABLE provider_event_sources (
    source_event_id  text PRIMARY KEY,
    event_id         uuid NOT NULL REFERENCES provider_events(id),
    received_at      timestamptz NOT NULL
);
