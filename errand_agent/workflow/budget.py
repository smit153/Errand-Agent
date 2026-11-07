"""Hard limits: per-run counters/deadline and per-operation lifetime caps (DECISIONS §2).

Counters are reserved durably *before* the work they pay for (conditional UPDATE … RETURNING,
so a reservation either succeeds atomically or raises). A new run never resets an operation's
lifetime allowance; the DB CHECK constraints are the backstop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, get_args
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from errand_agent import trace
from errand_agent.claims import Claim
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.errors import BudgetExhaustedError

RunCounter = Literal["logical_model_calls", "physical_model_calls", "tool_invocations", "embedding_calls"]


class RunLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    logical_model_calls: int = Field(default=10, ge=0)
    physical_model_calls: int = Field(default=40, ge=0)
    tool_invocations: int = Field(default=20, ge=0)
    embedding_calls: int = Field(default=4, ge=0)
    active_seconds: int = Field(default=300, ge=1)


class OperationLimits(BaseModel):
    """Lifetime caps across all runs and restarts. Must not exceed the DB CHECK constraints."""

    model_config = ConfigDict(frozen=True)

    dispatches: int = Field(default=3, ge=1, le=3)
    reconciliations: int = Field(default=6, ge=1, le=6)


class Run(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: int
    claim: Claim
    started_at: datetime
    deadline_at: datetime
    limits: RunLimits


class RunManager:
    def __init__(self, db: Database, clock: Clock, limits: RunLimits | None = None) -> None:
        self._db = db
        self._clock = clock
        self.limits = limits or RunLimits()

    def start(self, claim: Claim) -> Run:
        now = self._clock.now()
        run = Run(
            id=uuid4(),
            session_id=claim.session_id,
            claim=claim,
            started_at=now,
            deadline_at=now + timedelta(seconds=self.limits.active_seconds),
            limits=self.limits,
        )
        with self._db.session_txn(claim.session_id) as conn:
            conn.execute(
                "INSERT INTO runs (id, session_id, claim_generation, started_at, deadline_at, limits)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (run.id, run.session_id, claim.generation, now, run.deadline_at, Jsonb(self.limits.model_dump())),
            )
            trace.record(
                conn,
                "run.started",
                {"limits": self.limits.model_dump(), "generation": claim.generation},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
            )
        return run

    def end(self, run: Run, exit_reason: str) -> None:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            row = fetch_one(
                conn,
                "UPDATE runs SET ended_at = %s, exit_reason = %s WHERE id = %s AND ended_at IS NULL"
                " RETURNING logical_model_calls, physical_model_calls, tool_invocations, embedding_calls",
                (now, exit_reason, run.id),
            )
            trace.record(
                conn,
                "run.ended",
                {"exit_reason": exit_reason, "counters": dict(row) if row else None},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
            )


def reserve_run(conn: Conn, run: Run, counter: RunCounter, now: datetime) -> None:
    """Inside a session transaction: check the deadline, then atomically take one unit of `counter`."""
    if now >= run.deadline_at:
        raise BudgetExhaustedError("active_time", f"run {run.id} passed its {run.limits.active_seconds}s deadline")
    if counter not in get_args(RunCounter):
        raise ValueError(f"unknown run counter {counter!r}")
    limit = getattr(run.limits, counter)
    query = sql.SQL("UPDATE runs SET {c} = {c} + 1 WHERE id = %s AND {c} < %s RETURNING {c}").format(
        c=sql.Identifier(counter)
    )
    row = conn.execute(query, (run.id, limit)).fetchone()
    if row is None:
        raise BudgetExhaustedError(counter, f"run {run.id} reached {counter}={limit}")
