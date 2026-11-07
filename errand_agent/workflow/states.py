"""Explicit session state machine. Every transition is validated, CAS-versioned and traced."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args
from uuid import UUID

from errand_agent import trace
from errand_agent.db import Conn, fetch_one
from errand_agent.errors import InvalidTransitionError

SessionState = Literal[
    "collecting",
    "comparing",
    "held",
    "awaiting_approval",
    "executing",
    "verifying",
    "retry_wait",
    "pending_verification",
    "conflict",
    "completed",
    "failed",
]
STATES: tuple[SessionState, ...] = get_args(SessionState)

# Where a new proposal may be created from: anything that is not mid-execution.
PROPOSABLE: frozenset[SessionState] = frozenset(
    {"collecting", "comparing", "held", "awaiting_approval", "conflict", "completed", "failed"}
)

TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    "collecting": frozenset({"comparing", "awaiting_approval"}),
    "comparing": frozenset({"collecting", "held", "awaiting_approval"}),
    "held": frozenset({"comparing", "collecting", "awaiting_approval"}),
    "awaiting_approval": frozenset({"awaiting_approval", "executing", "held", "comparing", "collecting"}),
    "executing": frozenset({"verifying", "retry_wait", "completed", "failed", "conflict", "pending_verification"}),
    "verifying": frozenset({"executing", "completed", "failed", "conflict", "pending_verification"}),
    "retry_wait": frozenset({"executing", "pending_verification", "failed"}),
    "pending_verification": frozenset({"verifying", "executing", "completed", "failed", "conflict"}),
    "conflict": frozenset({"comparing", "collecting", "awaiting_approval"}),
    "completed": frozenset({"collecting", "comparing", "awaiting_approval"}),
    "failed": frozenset({"collecting", "comparing", "awaiting_approval"}),
}


def current_state(conn: Conn, session_id: int) -> tuple[SessionState, int]:
    row = fetch_one(conn, "SELECT state, revision FROM sessions WHERE id = %s", (session_id,))
    if row is None:
        raise InvalidTransitionError(f"unknown session {session_id}")
    return row["state"], int(row["revision"])


def transition(
    conn: Conn,
    session_id: int,
    to: SessionState,
    *,
    now: datetime,
    reason: str,
    run_id: UUID | None = None,
    operation_id: UUID | None = None,
) -> int:
    """Move the session to `to` (caller holds the session lock). Returns the new revision."""
    state, revision = current_state(conn, session_id)
    if to not in TRANSITIONS[state]:
        raise InvalidTransitionError(f"session {session_id}: {state} -> {to} is not allowed ({reason})")
    updated = conn.execute(
        "UPDATE sessions SET state = %s, revision = revision + 1, updated_at = %s WHERE id = %s AND revision = %s",
        (to, now, session_id, revision),
    ).rowcount
    if updated != 1:
        raise InvalidTransitionError(f"session {session_id} changed concurrently during {state} -> {to}")
    trace.record(
        conn,
        "state.transition",
        {"from": state, "to": to, "reason": reason, "revision": revision + 1},
        at=now,
        run_id=run_id,
        session_id=session_id,
        operation_id=operation_id,
    )
    return revision + 1
