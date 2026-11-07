"""Trace/journal events, written inside the same transaction as the state change they describe."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from errand_agent.db import Conn

MAX_TEXT = 500


def sanitize(value: Any) -> Any:
    """Bound free text and mark nothing as trusted; traces are audit data, never prompt input."""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + f"…[+{len(value) - MAX_TEXT} chars]"
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize(v) for v in value]
    return value


def record(
    conn: Conn,
    kind: str,
    payload: dict[str, Any],
    *,
    at: datetime,
    run_id: UUID | None = None,
    session_id: int | None = None,
    operation_id: UUID | None = None,
) -> None:
    conn.execute(
        "INSERT INTO trace_events (at, run_id, session_id, operation_id, kind, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (at, run_id, session_id, operation_id, kind, Jsonb(sanitize(payload))),
    )
