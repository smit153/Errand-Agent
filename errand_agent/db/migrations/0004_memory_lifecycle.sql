-- 0004: memory lifecycle invariants (stage 5a; DECISIONS §§7, 10, 18).
--
-- Typed fields only. A user-visible per-user number for inspect/correct/forget. Database checks
-- make the lifecycle rules hold even if application code is wrong:
--   * a retrievable (active) fact always has a vector of the current model;
--   * unconfirmed and pending facts carry no vector, so they can never be ranked;
--   * forgotten facts keep no value, vector or hash;
--   * one current fact per (user, field, scope), except free-text notes.

-- No memory rows exist before stage 5, so the column can be NOT NULL immediately.
ALTER TABLE memories ADD COLUMN display_no integer NOT NULL;
ALTER TABLE memories ADD CONSTRAINT memories_display_no_unique UNIQUE (user_id, display_no);
ALTER TABLE memories ADD CONSTRAINT memories_display_no_positive CHECK (display_no >= 1);

ALTER TABLE memories ADD CONSTRAINT memories_field_check
    CHECK (field IN ('area', 'earliest_time', 'preferred_provider', 'note'));
ALTER TABLE memories ADD CONSTRAINT memories_scope_check
    CHECK (scope IN ('global', 'plumber', 'electrician', 'ac_servicing', 'carpenter', 'cleaning'));
ALTER TABLE memories ADD CONSTRAINT memories_value_kind
    CHECK (status = 'forgotten' OR value->>'kind' = field);

-- Retrievable facts are confirmed and embedded with a recorded model and content hash.
ALTER TABLE memories ADD CONSTRAINT memories_active_embedded
    CHECK (status <> 'active' OR (confirmed AND embedding IS NOT NULL AND embedding_model IS NOT NULL
                                  AND content_hash IS NOT NULL));
-- Facts that must never be ranked carry no vector at all.
ALTER TABLE memories ADD CONSTRAINT memories_unranked_no_vector
    CHECK (status NOT IN ('unconfirmed', 'pending_embedding', 'superseded', 'forgotten') OR embedding IS NULL);
ALTER TABLE memories ADD CONSTRAINT memories_unconfirmed_is_inferred
    CHECK (status <> 'unconfirmed' OR (origin = 'inferred' AND NOT confirmed));
ALTER TABLE memories ADD CONSTRAINT memories_explicit_confirmed
    CHECK (origin <> 'explicit' OR confirmed);
ALTER TABLE memories ADD CONSTRAINT memories_forgotten_empty
    CHECK (status <> 'forgotten' OR (value = 'null'::jsonb AND content_hash IS NULL AND embedding_model IS NULL));

CREATE UNIQUE INDEX memories_one_current_fact ON memories (user_id, field, scope)
    WHERE status IN ('active', 'unconfirmed', 'pending_embedding') AND field <> 'note';
