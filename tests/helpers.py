"""Test helpers that build journal rows directly, for testing the backend in isolation.

Stage 3 creates these rows through the approval gate; backend tests only need a valid fence.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from errand_agent.claims import Claim
from errand_agent.db import Database
from errand_agent.tools.context import Fence


def make_operation(
    db: Database,
    *,
    session_id: int,
    user_id: str,
    action: str,
    snapshot: dict[str, Any],
    key: str,
    now: datetime,
) -> uuid.UUID:
    pid, rid, aid, oid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.txn() as conn:
        rev = conn.execute(
            "SELECT coalesce(max(revision), 0) + 1 AS r FROM proposals WHERE session_id = %s", (session_id,)
        ).fetchone()
        assert rev is not None
        conn.execute(
            "INSERT INTO proposals (id, session_id, revision, action, snapshot, snapshot_hash, token, status,"
            " created_at) VALUES (%s, %s, %s, %s, %s, 'h', %s, 'approved', %s)",
            (pid, session_id, rev["r"], action, Jsonb(snapshot), f"T{pid.hex[:10]}", now),
        )
        conn.execute(
            "INSERT INTO display_receipts (id, session_id, proposal_id, displayed_at) VALUES (%s, %s, %s, %s)",
            (rid, session_id, pid, now),
        )
        conn.execute(
            "INSERT INTO approvals (id, proposal_id, session_id, receipt_id, form, approved_at)"
            " VALUES (%s, %s, %s, %s, 'yes', %s)",
            (aid, pid, session_id, rid, now),
        )
        conn.execute(
            "INSERT INTO operations (id, session_id, user_id, proposal_id, approval_id, action, snapshot,"
            " idempotency_key, status, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'executing', %s, %s)",
            (oid, session_id, user_id, pid, aid, action, Jsonb(snapshot), key, now, now),
        )
    return oid


def reserve_attempt(db: Database, claim: Claim, operation_id: uuid.UUID, tool: str, now: datetime) -> Fence:
    attempt_id = uuid.uuid4()
    with db.txn() as conn:
        row = conn.execute(
            "SELECT coalesce(max(ordinal), 0) + 1 AS n FROM operation_attempts"
            " WHERE operation_id = %s AND kind = 'dispatch'",
            (operation_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO operation_attempts (id, operation_id, kind, ordinal, tool_name, claim_generation, status,"
            " started_at) VALUES (%s, %s, 'dispatch', %s, %s, %s, 'reserved', %s)",
            (attempt_id, operation_id, row["n"], tool, claim.generation, now),
        )
    return Fence(holder=claim.holder, generation=claim.generation, operation_id=operation_id, attempt_id=attempt_id)


def count(db: Database, query: str, params: tuple[Any, ...] = ()) -> int:
    with db.txn() as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(next(iter(row.values())))
