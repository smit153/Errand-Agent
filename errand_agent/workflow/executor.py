"""Operation executor: exact-snapshot dispatch, stable keys, forced verification, bounded retries.

Invariants (DECISIONS §§1, 2, 9, 11, 12-G7):
- Only an operation created by the approval gate can be executed (FK + unique constraints).
- A model-proposed call must agree exactly with the approved snapshot; any mismatch rejects the
  whole proposal with zero effect. Dispatch always uses the stored snapshot, never model arguments.
- Every dispatch reserves a durable attempt (lifetime + run counters) *before* the tool call, and
  carries a fence the backend re-verifies. No transaction spans a tool call.
- Success is never reported until an authoritative check confirms it: get_booking_status for
  book (forced after every dispatch, including timeouts), list_bookings for cancel/reschedule.
- A same-key retry happens only after the authoritative check says the effect did not happen.
- Every exit returns an ExecutionOutcome with a code-rendered message; unknown stays unknown.
- Once the model's proposal passes the snapshot check, the same transaction records it and
  creates an `operation_execute` job claimed by this run. If the process dies mid-dispatch, the
  claim expires and a background worker resumes the operation (reconciling first) without asking
  the model again; continuations (retries, reconciliation) are queued jobs too.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent import queue, trace
from errand_agent.claims import verify_claim
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.errors import BudgetExhaustedError, ErrandError, StaleClaimError
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import Fence, ToolContext
from errand_agent.tools.errors import ToolError, ToolRejectedError, ToolTransientError
from errand_agent.tools.models import BookingRecord
from errand_agent.workflow import templates
from errand_agent.workflow.budget import (
    OperationLimits,
    Run,
    charge_operation_time,
    check_operation_time,
    reserve_run,
)
from errand_agent.workflow.snapshots import (
    SNAPSHOT,
    BookSnapshot,
    CancelSnapshot,
    RescheduleSnapshot,
    normalize_arg,
)
from errand_agent.workflow.states import SessionState, current_state, transition

OperationStatus = Literal[
    "approved", "executing", "verifying", "retry_wait", "succeeded", "failed", "pending_verification"
]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed"})
AnySnapshot = BookSnapshot | CancelSnapshot | RescheduleSnapshot
DispatchEffect = Literal["returned", "no_effect", "rejected", "unknown"]
ExecFault = Callable[[str, UUID], None]  # (point, operation_id); may raise to simulate a crash
EXECUTE_LEASE_MARGIN = timedelta(seconds=30)
RECONCILE_DELAY = timedelta(seconds=30)
STATUS_FAILURES_PER_DRIVE = 2  # back-to-back failed status checks before waiting for a queued follow-up (BUG-008)
MAX_STEPS = 32  # loop guard; real bounds are the lifetime/run caps
ORPHAN_CHARGE_CAP_MS = 60_000  # a crashed call is charged at most the per-call bound


class OperationNotFoundError(ErrandError):
    user_message = "I couldn't find an approved action to carry out, so I haven't done anything."


class ProposedCall(BaseModel):
    """A model-proposed consequential tool call. Untrusted: validated, never executed as given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any]


class ExecutionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: UUID
    status: OperationStatus
    result_code: str
    message: str
    verified: bool
    booking_id: str | None = None


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: int
    user_id: str
    action: Literal["book", "cancel", "reschedule"]
    snapshot: AnySnapshot
    idempotency_key: str
    status: OperationStatus
    result_code: str | None
    outcome: dict[str, Any] | None
    dispatch_count: int
    reconcile_count: int
    next_retry_at: datetime | None
    proposal_checked_at: datetime | None = None


class OperationExecutor:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        tools: ToolClient,
        *,
        limits: OperationLimits | None = None,
        retry_delays_s: tuple[int, ...] = (1, 2),
        fault: ExecFault | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._tools = tools
        self._limits = limits or OperationLimits()
        self._retry_delays = retry_delays_s
        self.fault = fault

    # ================================================================== entry points
    def execute(self, run: Run, operation_id: UUID, proposal: ProposedCall) -> ExecutionOutcome:
        """Validate the model's proposed call against the approved snapshot, then drive the operation."""
        now = self._clock.now()
        job_id: UUID | None = None
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            op = _load(conn, operation_id, run.session_id)
            if op.status in TERMINAL:
                return _stored(op)
            if op.status == "approved" and op.proposal_checked_at is None:
                problem = check_proposal(op.action, op.snapshot, op.idempotency_key, proposal)
                trace.record(
                    conn,
                    "guardrail.snapshot_check",
                    {"proposed": proposal.model_dump(mode="json"), "accepted": problem is None, "problem": problem},
                    at=now,
                    run_id=run.id,
                    session_id=run.session_id,
                    operation_id=op.id,
                )
                if problem is not None:
                    return ExecutionOutcome(
                        operation_id=op.id,
                        status=op.status,
                        result_code="proposal_rejected",
                        message=templates.PROPOSAL_REJECTED,
                        verified=False,
                    )
                conn.execute("UPDATE operations SET proposal_checked_at = %s WHERE id = %s", (now, op.id))
                job_id = queue.enqueue(
                    conn,
                    kind="operation_execute",
                    session_id=run.session_id,
                    operation_id=op.id,
                    due_at=now,
                    now=now,
                    claimed_by=run.claim.holder,
                    claim_expires_at=run.deadline_at + EXECUTE_LEASE_MARGIN,
                )
        outcome = self._after_drive(run, self._drive(run, operation_id))
        if job_id is not None:
            with self._db.txn() as conn:
                queue.finish(conn, job_id, holder=run.claim.holder, now=self._clock.now())
        return outcome

    def resume(self, run: Run, operation_id: UUID) -> ExecutionOutcome:
        """Continue an already-validated operation (due retry, recovery). Never re-asks the model.

        An `approved` operation whose proposal never passed the snapshot check is not dispatched.
        """
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, self._clock.now())
            op = _load(conn, operation_id, run.session_id)
        if op.status == "approved" and op.proposal_checked_at is None:
            return ExecutionOutcome(
                operation_id=op.id,
                status="approved",
                result_code="awaiting_valid_proposal",
                message=templates.AWAITING_PROPOSAL,
                verified=False,
            )
        return self._after_drive(run, self._drive(run, operation_id))

    def _after_drive(self, run: Run, outcome: ExecutionOutcome) -> ExecutionOutcome:
        """An unresolved outcome always has a queued follow-up while lifetime allowance remains."""
        if outcome.status != "pending_verification":
            return outcome
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            row = fetch_one(
                conn, "SELECT reconcile_count, active_ms FROM operations WHERE id = %s", (outcome.operation_id,)
            )
            if (
                row is not None
                and row["reconcile_count"] < self._limits.reconciliations
                and row["active_ms"] < self._limits.active_seconds * 1000
            ):
                queued = queue.enqueue(
                    conn,
                    kind="operation_reconcile",
                    session_id=run.session_id,
                    operation_id=outcome.operation_id,
                    due_at=now + RECONCILE_DELAY,
                    now=now,
                    handover_from=run.claim.holder,
                )
                trace.record(
                    conn,
                    "operation.reconcile_scheduled",
                    {"job_id": str(queued) if queued else None, "due_at": (now + RECONCILE_DELAY).isoformat()},
                    at=now,
                    run_id=run.id,
                    session_id=run.session_id,
                    operation_id=outcome.operation_id,
                )
        return outcome

    # ================================================================== main loop
    def _drive(self, run: Run, operation_id: UUID) -> ExecutionOutcome:
        status_failures = 0
        try:
            for _ in range(MAX_STEPS):
                op = self._read(run, operation_id)
                now = self._clock.now()
                if op.status in TERMINAL:
                    return _stored(op)
                if op.status == "retry_wait":
                    if op.next_retry_at is not None and now < op.next_retry_at:
                        return ExecutionOutcome(
                            operation_id=op.id,
                            status="retry_wait",
                            result_code="retry_scheduled",
                            message=templates.retry_scheduled(op.action),
                            verified=True,
                        )
                    self._dispatch(run, op)
                elif op.status in ("verifying", "pending_verification") or self._needs_reconcile(op):
                    if not self._reconcile(run, op):
                        status_failures += 1
                        if status_failures >= STATUS_FAILURES_PER_DRIVE:
                            # Don't spend the lifetime allowance in a tight loop against a failing
                            # status service; the queued follow-up checks again later (_after_drive).
                            return ExecutionOutcome(
                                operation_id=op.id,
                                status="pending_verification",
                                result_code="status_unavailable",
                                message=templates.pending_verification(op.action),
                                verified=False,
                            )
                else:  # approved (validated) or executing after a verified "not done"
                    self._dispatch(run, op)
            raise ErrandError(f"operation {operation_id} exceeded {MAX_STEPS} steps")
        except BudgetExhaustedError as exc:
            return self._on_budget_exhausted(run, operation_id, exc)

    # ================================================================== dispatch
    def _dispatch(self, run: Run, op: _Op) -> None:
        fence = self._reserve_dispatch(run, op)
        if self.fault:
            self.fault("after_reserve", op.id)
        ctx = ToolContext(user_id=op.user_id, session_id=op.session_id, run_id=run.id, fence=fence)
        args = op.snapshot.dispatch_args(op.idempotency_key)
        result: dict[str, Any] | None = None
        error: ToolError | None = None
        effect: DispatchEffect
        try:
            if op.action == "book":
                result = self._tools.book(ctx, args).model_dump(mode="json")
            elif op.action == "cancel":
                result = self._tools.cancel(ctx, args).model_dump(mode="json")
            else:
                result = self._tools.reschedule(ctx, args).model_dump(mode="json")
            effect = "returned"
        except ToolTransientError as exc:
            error, effect = exc, "no_effect"
        except ToolRejectedError as exc:
            if exc.code == "fenced":
                raise StaleClaimError(f"backend fenced dispatch for operation {op.id}") from exc
            error, effect = exc, "rejected"
        except ToolError as exc:  # timeout or any other failure: the effect may have happened
            error, effect = exc, "unknown"
        if self.fault:
            self.fault("after_dispatch", op.id)
        self._record_dispatch(run, op, fence=fence, effect=effect, result=result, error=error)

    def _reserve_dispatch(self, run: Run, op: _Op) -> Fence:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            fresh = _load(conn, op.id, run.session_id)
            if fresh.dispatch_count >= self._limits.dispatches:
                raise BudgetExhaustedError(
                    "operation_dispatches", f"operation {op.id} used all {self._limits.dispatches} dispatches"
                )
            reserve_run(conn, run, "tool_invocations", now)
            check_operation_time(conn, op.id, self._limits)  # after the run deadline check
            attempt_id = uuid4()
            ordinal = fresh.dispatch_count + 1
            conn.execute(
                "INSERT INTO operation_attempts (id, operation_id, kind, ordinal, tool_name, run_id,"
                " claim_generation, status, started_at) VALUES (%s, %s, 'dispatch', %s, %s, %s, %s, 'reserved', %s)",
                (attempt_id, op.id, ordinal, op.action, run.id, run.claim.generation, now),
            )
            _update_op(conn, op.id, now, status="executing", dispatch_count=ordinal, next_retry_at=None)
            _ensure_state(conn, run, "executing", now=now, operation_id=op.id, reason=f"dispatch {ordinal}")
            trace.record(
                conn,
                "operation.dispatch_reserved",
                {"attempt_id": str(attempt_id), "ordinal": ordinal, "idempotency_key": op.idempotency_key},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
                operation_id=op.id,
            )
        return Fence(
            holder=run.claim.holder, generation=run.claim.generation, operation_id=op.id, attempt_id=attempt_id
        )

    def _record_dispatch(
        self,
        run: Run,
        op: _Op,
        *,
        fence: Fence,
        effect: DispatchEffect,
        result: dict[str, Any] | None,
        error: ToolError | None,
    ) -> None:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)  # a fenced worker's late result is discarded here
            self._charge_attempt(conn, op.id, fence.attempt_id, now)
            attempt_status = {
                "returned": "completed",
                "no_effect": "failed",
                "rejected": "failed",
                "unknown": "unknown",
            }
            payload = result if result is not None else _error_payload(error)
            conn.execute(
                "UPDATE operation_attempts SET status = %s, outcome = %s, finished_at = %s WHERE id = %s",
                (attempt_status[effect], Jsonb({"effect": effect, **payload}), now, fence.attempt_id),
            )
            trace.record(
                conn,
                "operation.dispatch_recorded",
                {"attempt_id": str(fence.attempt_id), "effect": effect, "result": payload},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
                operation_id=op.id,
            )
            if effect == "no_effect":
                # Use the reserved attempt's ordinal, not the pre-reservation snapshot (BUG-001).
                attempt = fetch_one(conn, "SELECT ordinal FROM operation_attempts WHERE id = %s", (fence.attempt_id,))
                ordinal = int(attempt["ordinal"]) if attempt else 1
                delay = self._retry_delays[min(ordinal, len(self._retry_delays)) - 1]
                due = now + timedelta(seconds=delay)
                _update_op(conn, op.id, now, status="retry_wait", next_retry_at=due)
                _ensure_state(
                    conn, run, "retry_wait", now=now, operation_id=op.id, reason="transient failure; retry scheduled"
                )
                queue.enqueue(
                    conn,
                    kind="operation_retry",
                    session_id=op.session_id,
                    operation_id=op.id,
                    due_at=due,
                    now=now,
                    handover_from=run.claim.holder,
                )
            elif effect == "rejected" and error is not None:
                # Rejected before any effect; earlier attempts were already reconciled as not done.
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="failed",
                    code=error.code,
                    state="failed",
                    message=templates.not_done(op.action, op.snapshot, error.code),
                    verified=True,
                )
            else:  # returned or unknown: always verify against the source of truth before deciding
                _update_op(conn, op.id, now, status="verifying")
                _ensure_state(conn, run, "verifying", now=now, operation_id=op.id, reason=f"verify after {effect}")

    # ================================================================== reconciliation
    def _needs_reconcile(self, op: _Op) -> bool:
        with self._db.txn() as conn:
            row = fetch_one(
                conn,
                "SELECT 1 AS x FROM operation_attempts WHERE operation_id = %s AND kind = 'dispatch'"
                " AND status IN ('reserved', 'unknown') LIMIT 1",
                (op.id,),
            )
        return row is not None

    def _reconcile(self, run: Run, op: _Op) -> bool:
        """One authoritative status check. False if the status tool itself failed."""
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            fresh = _load(conn, op.id, run.session_id)
            if fresh.reconcile_count >= self._limits.reconciliations:
                raise BudgetExhaustedError(
                    "operation_reconciliations",
                    f"operation {op.id} used all {self._limits.reconciliations} reconciliations",
                )
            reserve_run(conn, run, "tool_invocations", now)
            check_operation_time(conn, op.id, self._limits)  # after the run deadline check
            # Any attempt still reserved/unknown may have reached the backend: it is "unknown" until checked.
            # A crashed attempt's time is charged conservatively, up to the per-call bound.
            orphans = conn.execute(
                "UPDATE operation_attempts SET status = 'unknown', finished_at = coalesce(finished_at, %s)"
                " WHERE operation_id = %s AND kind = 'dispatch' AND status = 'reserved' RETURNING started_at",
                (now, op.id),
            ).fetchall()
            for orphan in orphans:
                elapsed = int((now - orphan["started_at"]).total_seconds() * 1000)
                charge_operation_time(conn, op.id, min(elapsed, ORPHAN_CHARGE_CAP_MS), self._limits)
            ordinal = fresh.reconcile_count + 1
            attempt_id = uuid4()
            tool = "get_booking_status" if op.action == "book" else "list_bookings"
            conn.execute(
                "INSERT INTO operation_attempts (id, operation_id, kind, ordinal, tool_name, run_id,"
                " claim_generation, status, started_at) VALUES (%s, %s, 'reconcile', %s, %s, %s, %s, 'reserved', %s)",
                (attempt_id, op.id, ordinal, tool, run.id, run.claim.generation, now),
            )
            _update_op(conn, op.id, now, reconcile_count=ordinal)
        ctx = ToolContext(user_id=op.user_id, session_id=op.session_id, run_id=run.id, operation_id=op.id)
        try:
            if op.action == "book":
                observed: dict[str, Any] = self._tools.get_booking_status(
                    ctx, {"idempotency_key": op.idempotency_key}
                ).model_dump(mode="json")
            else:
                listed = self._tools.list_bookings(ctx, {"user_id": op.user_id})
                target = op.snapshot.booking_id if not isinstance(op.snapshot, BookSnapshot) else ""
                found = next((b for b in listed.bookings if b.booking_id == target), None)
                observed = {"booking": found.model_dump(mode="json") if found else None}
        except ToolError as exc:
            self._record_reconcile_failure(run, op, attempt_id, exc)
            return False
        self._decide(run, op, attempt_id, observed)
        return True

    def _record_reconcile_failure(self, run: Run, op: _Op, attempt_id: UUID, exc: ToolError) -> None:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            self._charge_attempt(conn, op.id, attempt_id, now)
            conn.execute(
                "UPDATE operation_attempts SET status = 'failed', outcome = %s, finished_at = %s WHERE id = %s",
                (Jsonb(_error_payload(exc)), now, attempt_id),
            )
            _update_op(conn, op.id, now, status="pending_verification")
            _ensure_state(conn, run, "pending_verification", now=now, operation_id=op.id, reason="status check failed")

    def _decide(self, run: Run, op: _Op, attempt_id: UUID, observed: dict[str, Any]) -> None:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            self._charge_attempt(conn, op.id, attempt_id, now)
            fresh = _load(conn, op.id, run.session_id)
            conn.execute(
                "UPDATE operation_attempts SET status = 'completed', outcome = %s, finished_at = %s WHERE id = %s",
                (Jsonb(observed), now, attempt_id),
            )
            # The authoritative observation resolves every earlier unknown dispatch (BUG-002).
            conn.execute(
                "UPDATE operation_attempts SET status = 'reconciled'"
                " WHERE operation_id = %s AND kind = 'dispatch' AND status = 'unknown'",
                (op.id,),
            )
            trace.record(
                conn,
                "operation.reconciled",
                {"attempt_id": str(attempt_id), "observed": observed},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
                operation_id=op.id,
            )
            last = _last_dispatch(conn, op.id)
            if isinstance(fresh.snapshot, BookSnapshot):
                self._decide_book(conn, run, fresh, snap=fresh.snapshot, observed=observed, now=now)
            else:
                booking = BookingRecord.model_validate(observed["booking"]) if observed.get("booking") else None
                self._decide_booking_change(conn, run, fresh, booking=booking, last=last, now=now)

    def _decide_book(
        self, conn: Conn, run: Run, op: _Op, *, snap: BookSnapshot, observed: dict[str, Any], now: datetime
    ) -> None:
        status = observed.get("status")
        if status == "booked":
            booking_id = observed.get("booking_id")
            self._finish(
                conn,
                run,
                op,
                now,
                status="succeeded",
                code="booked",
                state="completed",
                message=templates.booking_confirmed(snap, str(booking_id)),
                verified=True,
                booking_id=booking_id,
            )
        elif status == "slot_taken":
            self._finish(
                conn,
                run,
                op,
                now,
                status="failed",
                code="slot_taken",
                state="conflict",
                message=templates.not_done("book", snap, "slot_taken"),
                verified=True,
            )
        elif status == "hold_expired" or now >= snap.hold_expires_at:
            self._finish(
                conn,
                run,
                op,
                now,
                status="failed",
                code="hold_expired",
                state="failed",
                message=templates.hold_expired(snap.slot),
                verified=True,
            )
        elif op.dispatch_count >= self._limits.dispatches:
            self._finish(
                conn,
                run,
                op,
                now,
                status="failed",
                code="retries_exhausted",
                state="failed",
                message=templates.not_done("book", snap, "retries_exhausted"),
                verified=True,
            )
        else:  # authoritatively not booked, hold still valid: same-key retry
            _update_op(conn, op.id, now, status="executing")
            _ensure_state(
                conn, run, "executing", now=now, operation_id=op.id, reason="verified not booked; same-key retry"
            )

    def _decide_booking_change(
        self,
        conn: Conn,
        run: Run,
        op: _Op,
        *,
        booking: BookingRecord | None,
        last: dict[str, Any] | None,
        now: datetime,
    ) -> None:
        snap = op.snapshot
        returned = (last or {}).get("status")
        if booking is None:
            _update_op(conn, op.id, now, status="pending_verification")
            _ensure_state(
                conn, run, "pending_verification", now=now, operation_id=op.id, reason="booking missing from history"
            )
            return
        if isinstance(snap, CancelSnapshot):
            if booking.status == "cancelled":
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="succeeded",
                    code="cancelled",
                    state="completed",
                    message=templates.cancelled(snap),
                    verified=True,
                    booking_id=booking.booking_id,
                )
            elif booking.status in ("cancelled_by_provider", "completed"):
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="failed",
                    code=booking.status,
                    state="failed",
                    message=templates.not_done("cancel", snap, booking.status),
                    verified=True,
                )
            else:
                self._retry_or_exhaust(conn, run, op, now)
            return
        if isinstance(snap, RescheduleSnapshot):
            if booking.status == "confirmed" and booking.slot == snap.slot:
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="succeeded",
                    code="rescheduled",
                    state="completed",
                    message=templates.rescheduled(snap),
                    verified=True,
                    booking_id=booking.booking_id,
                )
            elif booking.status == "confirmed" and booking.slot == snap.old_slot and returned == "slot_conflict":
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="failed",
                    code="slot_conflict",
                    state="conflict",
                    message=templates.not_done("reschedule", snap, "slot_conflict"),
                    verified=True,
                )
            elif booking.status == "confirmed" and booking.slot == snap.old_slot:
                self._retry_or_exhaust(conn, run, op, now)
            elif booking.status != "confirmed":
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="failed",
                    code=booking.status,
                    state="failed",
                    message=templates.not_done("reschedule", snap, booking.status),
                    verified=True,
                )
            else:
                _update_op(conn, op.id, now, status="pending_verification")
                _ensure_state(
                    conn,
                    run,
                    "pending_verification",
                    now=now,
                    operation_id=op.id,
                    reason="booking in an unexpected slot",
                )

    def _retry_or_exhaust(self, conn: Conn, run: Run, op: _Op, now: datetime) -> None:
        if op.dispatch_count >= self._limits.dispatches:
            self._finish(
                conn,
                run,
                op,
                now,
                status="failed",
                code="retries_exhausted",
                state="failed",
                message=templates.not_done(op.action, op.snapshot, "retries_exhausted"),
                verified=True,
            )
        else:
            _update_op(conn, op.id, now, status="executing")
            _ensure_state(
                conn, run, "executing", now=now, operation_id=op.id, reason="verified not done; same-key retry"
            )

    # ================================================================== exits
    def _finish(
        self,
        conn: Conn,
        run: Run,
        op: _Op,
        now: datetime,
        *,
        status: Literal["succeeded", "failed"],
        code: str,
        state: SessionState,
        message: str,
        verified: bool,
        booking_id: str | None = None,
    ) -> None:
        outcome = {"message": message, "verified": verified, "booking_id": booking_id}
        _update_op(conn, op.id, now, status=status, result_code=code, outcome=outcome, next_retry_at=None)
        _ensure_state(conn, run, state, now=now, operation_id=op.id, reason=f"operation {status}: {code}")
        trace.record(
            conn,
            "operation.finished",
            {"status": status, "result_code": code, "verified": verified, "booking_id": booking_id},
            at=now,
            run_id=run.id,
            session_id=run.session_id,
            operation_id=op.id,
        )

    def _on_budget_exhausted(self, run: Run, operation_id: UUID, exc: BudgetExhaustedError) -> ExecutionOutcome:
        """A limit stopped the loop. Unknown effects stay unknown; nothing is claimed either way."""
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, now)
            op = _load(conn, operation_id, run.session_id)
            trace.record(
                conn,
                "budget.exhausted",
                {"limit": exc.limit, "detail": str(exc)},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
                operation_id=op.id,
            )
            if op.status in TERMINAL:
                return _stored(op)
            unknown = (
                op.status in ("verifying", "pending_verification")
                or fetch_one(
                    conn,
                    "SELECT 1 AS x FROM operation_attempts WHERE operation_id = %s AND kind = 'dispatch'"
                    " AND status IN ('reserved', 'unknown') LIMIT 1",
                    (op.id,),
                )
                is not None
            )
            message: str
            status: OperationStatus
            verified: bool
            if op.status == "retry_wait":
                message, status, verified = templates.retry_scheduled(op.action), op.status, True
            elif unknown:
                _update_op(conn, op.id, now, status="pending_verification")
                _ensure_state(
                    conn, run, "pending_verification", now=now, operation_id=op.id, reason=f"limit {exc.limit}"
                )
                message, status, verified = (
                    templates.pending_verification(op.action, exc.limit),
                    "pending_verification",
                    False,
                )
            else:
                self._finish(
                    conn,
                    run,
                    op,
                    now,
                    status="failed",
                    code=f"limit_{exc.limit}",
                    state="failed",
                    message=templates.limit_not_done(op.action, exc.limit, dispatched=op.dispatch_count > 0),
                    verified=True,
                )
                return _stored(_load(conn, operation_id, run.session_id))
        return ExecutionOutcome(
            operation_id=operation_id,
            status=status,
            result_code=f"limit_{exc.limit}",
            message=message,
            verified=verified,
        )

    # ================================================================== helpers
    def _charge_attempt(self, conn: Conn, operation_id: UUID, attempt_id: UUID, now: datetime) -> None:
        row = fetch_one(conn, "SELECT started_at FROM operation_attempts WHERE id = %s", (attempt_id,))
        if row is not None:
            elapsed = int((now - row["started_at"]).total_seconds() * 1000)
            charge_operation_time(conn, operation_id, elapsed, self._limits)

    def _read(self, run: Run, operation_id: UUID) -> _Op:
        with self._db.session_txn(run.session_id) as conn:
            self._verify(conn, run, self._clock.now())
            return _load(conn, operation_id, run.session_id)

    def _verify(self, conn: Conn, run: Run, now: datetime) -> None:
        verify_claim(conn, run.session_id, run.claim.holder, run.claim.generation, now)


def check_proposal(action: str, snapshot: AnySnapshot, key: str, proposal: ProposedCall) -> str | None:
    """G7: exact agreement with the approved snapshot. Returns a problem description or None."""
    if proposal.tool != action:
        return f"proposed tool {proposal.tool!r} is not the approved action {action!r}"
    expected = snapshot.dispatch_args(key)
    args = dict(proposal.arguments)
    if "idempotency_key" in args and args.pop("idempotency_key") != key:
        return "idempotency_key differs from the approved operation"
    material = {k: v for k, v in expected.items() if k != "idempotency_key"}
    if set(args) != set(material):
        return f"arguments {sorted(args)} differ from the approved arguments {sorted(material)}"
    for name, value in material.items():
        if normalize_arg(args[name]) != normalize_arg(value):
            return f"{name} differs from the approved value"
    return None


def _load(conn: Conn, operation_id: UUID, session_id: int) -> _Op:
    row = fetch_one(conn, "SELECT * FROM operations WHERE id = %s AND session_id = %s", (operation_id, session_id))
    if row is None:
        raise OperationNotFoundError(f"operation {operation_id} not found in session {session_id}")
    return _Op(
        id=row["id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        action=row["action"],
        snapshot=SNAPSHOT.validate_python(row["snapshot"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        result_code=row["result_code"],
        outcome=row["outcome"],
        dispatch_count=row["dispatch_count"],
        reconcile_count=row["reconcile_count"],
        next_retry_at=row["next_retry_at"],
        proposal_checked_at=row["proposal_checked_at"],
    )


def _stored(op: _Op) -> ExecutionOutcome:
    outcome = op.outcome or {}
    return ExecutionOutcome(
        operation_id=op.id,
        status=op.status,
        result_code=op.result_code or op.status,
        message=str(outcome.get("message", "")),
        verified=bool(outcome.get("verified", False)),
        booking_id=outcome.get("booking_id"),
    )


_UPDATABLE = frozenset({"status", "result_code", "outcome", "dispatch_count", "reconcile_count", "next_retry_at"})


def _update_op(conn: Conn, operation_id: UUID, now: datetime, **fields: Any) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    names = sorted(fields)
    assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(n)) for n in names)
    params = [Jsonb(fields[n]) if n == "outcome" else fields[n] for n in names]
    conn.execute(
        sql.SQL("UPDATE operations SET {}, version = version + 1, updated_at = %s WHERE id = %s").format(assignments),
        (*params, now, operation_id),
    )


def _ensure_state(conn: Conn, run: Run, to: SessionState, *, now: datetime, operation_id: UUID, reason: str) -> None:
    state, _ = current_state(conn, run.session_id)
    if state != to:
        transition(conn, run.session_id, to, now=now, reason=reason, run_id=run.id, operation_id=operation_id)


def _last_dispatch(conn: Conn, operation_id: UUID) -> dict[str, Any] | None:
    row = fetch_one(
        conn,
        "SELECT outcome FROM operation_attempts WHERE operation_id = %s AND kind = 'dispatch'"
        " AND status = 'completed' ORDER BY ordinal DESC LIMIT 1",
        (operation_id,),
    )
    return row["outcome"] if row else None


def _error_payload(error: ToolError | None) -> dict[str, Any]:
    if error is None:
        return {}
    return {"error": type(error).__name__, "code": error.code, "effect": error.effect}
