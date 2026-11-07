-- 0007: HTTP request de-duplication (stage 8; DECISIONS §8). A client sends each chat message
-- with its own ID; a retried request (lost response, reconnect) gets the stored reply instead
-- of running the turn again. A NULL response marks a request still being handled.
CREATE TABLE api_requests (
    session_id         bigint NOT NULL REFERENCES sessions(id),
    client_message_id  text NOT NULL CHECK (client_message_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    text_hash          text NOT NULL,
    created_at         timestamptz NOT NULL,
    response           jsonb,
    PRIMARY KEY (session_id, client_message_id)
);
