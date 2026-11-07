-- 0003: code-owned per-session working data for the agent (displayed options, selection,
-- history target, degraded menu). Facts the user supplied stay in sessions.request.
ALTER TABLE sessions ADD COLUMN workspace jsonb NOT NULL DEFAULT '{}'::jsonb;
