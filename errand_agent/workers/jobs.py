"""Background job worker (DECISIONS §§8, 9, 11; E14, E31, E32).

Stateless and deterministic: no language model. It claims due jobs with `FOR UPDATE SKIP LOCKED`
(and jobs whose previous worker's lease expired), then for each job acquires the session claim,
starts a bounded run and resumes the operation through the same executor the agent uses. The
executor fences, reconciles before any retry and never dispatches a proposal that didn't pass the
snapshot check. If the session is busy (the user is mid-turn) the job is put back, not failed.

When background work changes an operation's outcome while the user is away, the user is told
through the outbox, keyed per operation and status so a re-run can't notify twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from errand_agent import queue, trace
from errand_agent.claims import ClaimManager
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.errors import SessionBusyError, StaleClaimError
from errand_agent.queue import Job
from errand_agent.workers.outbox import notify
from errand_agent.workflow import templates
from errand_agent.workflow.budget import OperationLimits, RunManager
from errand_agent.workflow.executor import TERMINAL, ExecutionOutcome, OperationExecutor

log = logging.getLogger(__name__)

LEASE = timedelta(seconds=360)  # longer than a run's 300 s active limit
BUSY_DELAY = timedelta(seconds=2)
MAX_JOB_ATTEMPTS = 5
_FAILURE_BACKOFF = (5, 15, 30, 60)


class JobResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: UUID
    kind: str
    status: Literal["done", "requeued", "failed", "lost"]
    detail: str


class EventHandler(Protocol):
    def handle(self, job: Job, holder: str) -> str: ...


class JobWorker:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        executor: OperationExecutor,
        *,
        holder: str,
        claims: ClaimManager,
        runs: RunManager,
        events: EventHandler | None = None,
        limits: OperationLimits | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._executor = executor
        self.holder = holder
        self._claims = claims
        self._runs = runs
        self._events = events
        self._limits = limits or OperationLimits()

    def run_due(self, *, limit: int = 10) -> list[JobResult]:
        with self._db.txn() as conn:
            jobs = queue.claim_due(conn, holder=self.holder, now=self._clock.now(), lease=LEASE, limit=limit)
        return [self.run(job) for job in jobs]

    def run(self, job: Job) -> JobResult:
        try:
            if job.kind == "provider_cancellation":
                if self._events is None:
                    return self._requeue(job, "no event handler configured", BUSY_DELAY, refund=True)
                detail = self._events.handle(job, self.holder)
            else:
                detail = self._operation(job)
        except SessionBusyError:
            return self._requeue(job, "session busy", BUSY_DELAY, refund=True)
        except StaleClaimError as exc:
            return self._requeue(job, f"fenced: {exc}", timedelta(0), refund=False)
        except Exception as exc:  # a bug or unexpected failure: recorded, retried with backoff, then failed
            log.exception("job %s (%s) failed", job.id, job.kind)
            return self._failed(job, f"{type(exc).__name__}: {exc}")
        with self._db.txn() as conn:
            kept = queue.finish(conn, job.id, holder=self.holder, now=self._clock.now())
        return JobResult(job_id=job.id, kind=job.kind, status="done" if kept else "lost", detail=detail)

    # ------------------------------------------------------------------ operations
    def _operation(self, job: Job) -> str:
        if job.session_id is None or job.operation_id is None:
            raise ValueError(f"{job.kind} job {job.id} has no session/operation")
        before = self._status(job.operation_id)
        if before in TERMINAL:
            return f"already {before}"
        claim = self._claims.acquire(job.session_id, self.holder)
        run = self._runs.start(claim)
        exit_reason = f"job:{job.kind}"
        try:
            with self._db.session_txn(job.session_id) as conn:
                trace.record(
                    conn,
                    "job.started",
                    {"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts, "holder": self.holder},
                    at=self._clock.now(),
                    run_id=run.id,
                    session_id=job.session_id,
                    operation_id=job.operation_id,
                )
            outcome = self._executor.resume(run, job.operation_id)
        except StaleClaimError:
            exit_reason = "fenced"
            raise
        finally:
            self._runs.end(run, exit_reason)
            self._claims.release(claim)
        self._ensure_retry_queued(job, outcome)
        self._tell_user(job, before, outcome)
        return f"{before} -> {outcome.status} ({outcome.result_code})"

    def _ensure_retry_queued(self, job: Job, outcome: ExecutionOutcome) -> None:
        """Safety net: an operation waiting for a retry always has a queued retry job."""
        if outcome.status != "retry_wait" or job.session_id is None:
            return
        with self._db.session_txn(job.session_id) as conn:
            row = fetch_one(conn, "SELECT next_retry_at FROM operations WHERE id = %s", (outcome.operation_id,))
            due = row["next_retry_at"] if row and row["next_retry_at"] else self._clock.now()
            queue.enqueue(
                conn,
                kind="operation_retry",
                session_id=job.session_id,
                operation_id=outcome.operation_id,
                due_at=due,
                now=self._clock.now(),
                handover_from=self.holder if job.kind == "operation_retry" else None,
            )

    def _tell_user(self, job: Job, before: str, outcome: ExecutionOutcome) -> None:
        """Notify only when this background run moved the operation to an outcome the user hasn't seen."""
        if job.session_id is None or (outcome.status == before and outcome.status != "pending_verification"):
            return
        with self._db.session_txn(job.session_id) as conn:
            op = fetch_one(conn, "SELECT user_id, action FROM operations WHERE id = %s", (outcome.operation_id,))
            if op is None:
                return
            now = self._clock.now()
            if outcome.status in TERMINAL:
                notify(
                    conn,
                    user_id=op["user_id"],
                    session_id=job.session_id,
                    event_key=f"operation:{outcome.operation_id}:{outcome.status}",
                    kind="operation_outcome",
                    text=templates.background_outcome(outcome.message),
                    now=now,
                )
            elif outcome.status == "pending_verification" and not self._reconcile_queued(
                conn, outcome.operation_id, job.id
            ):
                notify(
                    conn,
                    user_id=op["user_id"],
                    session_id=job.session_id,
                    event_key=f"operation:{outcome.operation_id}:pending_final",
                    kind="operation_pending",
                    text=templates.still_pending(op["action"]),
                    now=now,
                )

    @staticmethod
    def _reconcile_queued(conn: Conn, operation_id: UUID, running: UUID) -> bool:
        """Another follow-up is queued (the job asking doesn't count: it is about to finish)."""
        row = fetch_one(
            conn,
            "SELECT 1 AS x FROM jobs WHERE operation_id = %s AND kind = 'operation_reconcile'"
            " AND status IN ('pending', 'claimed') AND id <> %s",
            (operation_id, running),
        )
        return row is not None

    def _status(self, operation_id: UUID) -> str:
        with self._db.txn() as conn:
            row = fetch_one(conn, "SELECT status FROM operations WHERE id = %s", (operation_id,))
        if row is None:
            raise ValueError(f"operation {operation_id} not found")
        return str(row["status"])

    # ------------------------------------------------------------------ queue bookkeeping
    def _requeue(self, job: Job, why: str, delay: timedelta, *, refund: bool) -> JobResult:
        with self._db.txn() as conn:
            kept = queue.release(
                conn, job.id, holder=self.holder, due_at=self._clock.now() + delay, error=why, refund_attempt=refund
            )
        return JobResult(job_id=job.id, kind=job.kind, status="requeued" if kept else "lost", detail=why)

    def _failed(self, job: Job, error: str) -> JobResult:
        if job.attempts < MAX_JOB_ATTEMPTS:
            delay = timedelta(seconds=_FAILURE_BACKOFF[min(job.attempts, len(_FAILURE_BACKOFF)) - 1])
            return self._requeue(job, error, delay, refund=False)
        with self._db.txn() as conn:
            queue.finish(conn, job.id, holder=self.holder, now=self._clock.now(), status="failed", error=error[:500])
        return JobResult(job_id=job.id, kind=job.kind, status="failed", detail=error)
