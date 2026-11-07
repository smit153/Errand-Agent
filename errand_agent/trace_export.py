"""Export a conversation's trace as JSONL (PDF §3 observability, §5 raw trace deliverable).

One JSON object per line, in commit order: a header line with what was exported, then every
trace event of the session (runs, state transitions, model calls with exact context, tool calls
with arguments and sanitized results, approvals, operations, memory, jobs), then one summary line
per run with its counters. Events are exported as stored; nothing is rewritten.

    python -m errand_agent.trace_export --session 12 --out artifacts/traces/session-12.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from errand_agent.config import load_settings
from errand_agent.db import Database, fetch_all


def session_events(db: Database, session_id: int) -> list[dict[str, Any]]:
    with db.txn() as conn:
        events = fetch_all(
            conn,
            "SELECT id, at, run_id, session_id, operation_id, kind, payload FROM trace_events"
            " WHERE session_id = %s OR operation_id IN (SELECT id FROM operations WHERE session_id = %s)"
            " ORDER BY id",
            (session_id, session_id),
        )
        runs = fetch_all(
            conn,
            "SELECT id, started_at, ended_at, exit_reason, logical_model_calls, physical_model_calls,"
            " tool_invocations FROM runs WHERE session_id = %s ORDER BY started_at, id",
            (session_id,),
        )
    out = [{"type": "event", **_jsonable(dict(e))} for e in events]
    out += [{"type": "run_summary", **_jsonable(dict(r))} for r in runs]
    return out


def write_jsonl(path: Path, session_id: int, rows: list[dict[str, Any]], *, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "header",
        "session_id": session_id,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "events": sum(r["type"] == "event" for r in rows),
        "runs": sum(r["type"] == "run_summary" for r in rows),
        "note": note,
    }
    with path.open("w", encoding="utf-8") as fh:
        for row in [header, *rows]:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m errand_agent.trace_export", description=__doc__)
    parser.add_argument("--session", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db", help="database name (default from settings)")
    args = parser.parse_args(argv)
    db = Database(load_settings().conninfo(dbname=args.db))
    try:
        rows = session_events(db, args.session)
    finally:
        db.close()
    if not rows:
        print(f"session {args.session} has no trace", file=sys.stderr)
        return 1
    write_jsonl(args.out, args.session, rows)
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
