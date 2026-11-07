"""Postgres job queue primitives (DECISIONS §9). Plain SQL, no workflow imports.

Jobs are created in the same transaction as the state change that needs them, claimed with
`FOR UPDATE SKIP LOCKED`, and carry a lease: a job whose worker died is claimable again after
`claim_expires_at`. Claiming a job never takes a session lock, and no transaction holds a job row
while waiting for a session lock, so the queue can't deadlock with session work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent.db import Conn, fetch_all, fetch_one

JobKind = Literal["operation_execute", "operation_retry", "operation_reconcile", "provider_cancellation"]


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: JobKind
    session_id: int | None
    operation_id: UUID | None
    event_id: UUID | None
    due_at: datetime
    attempts: int
    claimed_by: str | None
    payload: dict[str, Any]


def enqueue(
    conn: Conn,
    *,
    kind: JobKind,
    due_at: datetime,
    now: datetime,
    session_id: int | None = None,
    operation_id: UUID | None = None,
    event_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    claimed_by: str | None = None,
    claim_expires_at: datetime | None = None,
    handover_from: str | None = None,
) -> UUID | None:
    """Create a job unless an open one of the same kind exists for the operation/event. Returns its ID.

    `handover_from`: a running job of the same kind for this operation, claimed by that holder, is
    closed first, so a job can schedule its own continuation (e.g. a retry after another 503) in
    the same transaction as the state change.
    """
    if handover_from is not None and operation_id is not None:
        conn.execute(
            "UPDATE jobs SET status = 'done', finished_at = %s, claim_expires_at = NULL"
            " WHERE kind = %s AND operation_id = %s AND status = 'claimed' AND claimed_by = %s",
            (now, kind, operation_id, handover_from),
        )
    open_job = fetch_one(
        conn,
        "SELECT id FROM jobs WHERE kind = %s AND status IN ('pending', 'claimed')"
        " AND ((%s::uuid IS NOT NULL AND operation_id = %s) OR (%s::uuid IS NOT NULL AND event_id = %s))",
        (kind, operation_id, operation_id, event_id, event_id),
    )
    if open_job is not None:
        return None
    job_id = uuid4()
    claimed = claimed_by is not None
    conn.execute(
        "INSERT INTO jobs (id, kind, session_id, operation_id, event_id, due_at, status, attempts, claimed_by,"
        " claim_expires_at, payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            job_id,
            kind,
            session_id,
            operation_id,
            event_id,
            due_at,
            "claimed" if claimed else "pending",
            1 if claimed else 0,
            claimed_by,
            claim_expires_at if claimed else None,
            Jsonb(payload or {}),
            now,
        ),
    )
    return job_id


def claim_due(conn: Conn, *, holder: str, now: datetime, lease: timedelta, limit: int) -> list[Job]:
    """Claim due pending jobs and jobs whose previous claim expired (a crashed worker)."""
    rows = fetch_all(
        conn,
        "UPDATE jobs SET status = 'claimed', claimed_by = %(holder)s, claim_expires_at = %(expires)s,"
        " attempts = attempts + 1 WHERE id IN ("
        "  SELECT id FROM jobs WHERE (status = 'pending' AND due_at <= %(now)s)"
        "   OR (status = 'claimed' AND claim_expires_at <= %(now)s)"
        "  ORDER BY due_at, id FOR UPDATE SKIP LOCKED LIMIT %(limit)s"
        ") RETURNING id, kind, session_id, operation_id, event_id, due_at, attempts, claimed_by, payload",
        {"holder": holder, "expires": now + lease, "now": now, "limit": limit},
    )
    return sorted((Job.model_validate(dict(r)) for r in rows), key=lambda j: (j.due_at, str(j.id)))


def finish(
    conn: Conn,
    job_id: UUID,
    *,
    holder: str,
    now: datetime,
    status: Literal["done", "failed"] = "done",
    error: str | None = None,
) -> bool:
    """Close a job this holder still owns (or already handed over). False if another worker took it over."""
    updated = conn.execute(
        "UPDATE jobs SET status = %s, finished_at = coalesce(finished_at, %s), last_error = %s,"
        " claim_expires_at = NULL WHERE id = %s AND claimed_by = %s"
        " AND (status = 'claimed' OR (status = 'done' AND %s = 'done'))",
        (status, now, error, job_id, holder, status),
    ).rowcount
    return updated == 1


def release(
    conn: Conn, job_id: UUID, *, holder: str, due_at: datetime, error: str | None = None, refund_attempt: bool = False
) -> bool:
    """Give a claimed job back to the queue, due again at `due_at`."""
    updated = conn.execute(
        "UPDATE jobs SET status = 'pending', due_at = %s, claimed_by = NULL, claim_expires_at = NULL,"
        " last_error = %s, attempts = attempts - %s WHERE id = %s AND status = 'claimed' AND claimed_by = %s",
        (due_at, error, 1 if refund_attempt else 0, job_id, holder),
    ).rowcount
    return updated == 1
