-- 0002: an "unknown" dispatch attempt becomes "reconciled" once an authoritative check has
-- observed the outcome, so later steps don't treat it as still unresolved (see docs/BUGS.md BUG-002).
ALTER TABLE operation_attempts DROP CONSTRAINT operation_attempts_status_check;
ALTER TABLE operation_attempts ADD CONSTRAINT operation_attempts_status_check
    CHECK (status IN ('reserved', 'completed', 'failed', 'unknown', 'reconciled', 'discarded'));
