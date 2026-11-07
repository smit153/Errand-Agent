"""Stage 3b invariants: exact-snapshot dispatch, forced verification, stable keys, bounded retries.

Key invariants:
- no booking side effect without an approved operation and an exactly matching proposal (E29);
- success is reported only after get_booking_status/list_bookings confirms it (E10, E27 basis);
- after a timeout the source of truth is queried before any retry; retries reuse the key (E11);
- lifetime and run caps stop the loop with a truthful outcome; unknown stays unknown (E23).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import HOLD_TTL, MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workflow.approval import ApprovalGate
from errand_agent.workflow.budget import Run, RunLimits, RunManager
from errand_agent.workflow.executor import ExecFault, OperationExecutor, OperationNotFoundError, ProposedCall
from errand_agent.workflow.snapshots import BookSnapshot, CancelSnapshot, RescheduleSnapshot
from tests.helpers import count
from tests.workflow_helpers import AC_BOOKING, book_snapshot, cancel_snapshot, reschedule_snapshot


@dataclass
class Harness:
    db: Database
    clock: FakeClock
    backend: MockBackend
    tools: ToolClient
    ctx: ToolContext
    gate: ApprovalGate
    claims: ClaimManager
    executor: OperationExecutor

    def run(self, limits: RunLimits | None = None, holder: str = "worker-1") -> Run:
        claim = self.claims.acquire(self.ctx.session_id, holder)
        return RunManager(self.db, self.clock, limits).start(claim)

    def approve(self, snap: BookSnapshot | CancelSnapshot | RescheduleSnapshot) -> UUID:
        proposal = self.gate.propose(self.ctx.session_id, snap)
        receipt = self.gate.display(self.ctx.session_id, proposal.id)
        result = self.gate.confirm(self.ctx.session_id, DEMO_USER, "yes", receipt.id)
        assert result.operation_id is not None
        return result.operation_id

    def tool_calls(self, tool: str) -> list[dict[str, Any]]:
        with self.db.txn() as conn:
            rows = conn.execute(
                "SELECT payload FROM trace_events WHERE kind = 'tool.call' AND payload->>'tool' = %s ORDER BY id",
                (tool,),
            ).fetchall()
        return [r["payload"] for r in rows]

    def op(self, operation_id: UUID) -> dict[str, Any]:
        with self.db.txn() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id = %s", (operation_id,)).fetchone()
        assert row is not None
        return dict(row)

    def session_state(self) -> str:
        with self.db.txn() as conn:
            row = conn.execute("SELECT state FROM sessions WHERE id = %s", (self.ctx.session_id,)).fetchone()
        assert row is not None
        return str(row["state"])

    def bookings_for(self, hold_id: str) -> int:
        return count(self.db, "SELECT count(*) FROM mock_bookings WHERE hold_id = %s", (hold_id,))


def make_harness(
    db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext, *, lease: int = 3600
) -> Harness:
    return Harness(
        db=db,
        clock=clock,
        backend=backend,
        tools=tools,
        ctx=ctx,
        gate=ApprovalGate(db, clock),
        claims=ClaimManager(db, clock, lease_seconds=lease),
        executor=OperationExecutor(db, clock, tools),
    )


@pytest.fixture
def h(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext) -> Harness:
    return make_harness(db, clock, backend, tools, ctx)


def advance_at(clock: FakeClock, point: str, delta: timedelta) -> ExecFault:
    def hook(at: str, _operation_id: UUID) -> None:
        if at == point:
            clock.advance(delta)

    return hook


def book_call(snap: BookSnapshot, **override: Any) -> ProposedCall:
    return ProposedCall(tool="book", arguments={"hold_id": snap.hold_id, **override})


# ------------------------------------------------------------------ E29: exact snapshot agreement
@pytest.mark.invariant
@pytest.mark.parametrize(
    "proposal",
    [
        "wrong_tool",
        "other_hold",
        "extra_argument",
        "wrong_key",
        "missing_hold",
    ],
)
def test_mismatched_proposal_is_rejected_with_zero_effect(h: Harness, proposal: str) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    other = book_snapshot(h.tools, h.ctx, nth_slot=1)
    op_id = h.approve(snap)
    call = {
        "wrong_tool": ProposedCall(tool="cancel", arguments={"booking_id": AC_BOOKING}),
        "other_hold": book_call(snap, hold_id=other.hold_id),
        "extra_argument": book_call(snap, price_inr=1),
        "wrong_key": book_call(snap, idempotency_key="op-forged"),
        "missing_hold": ProposedCall(tool="book", arguments={}),
    }[proposal]
    run = h.run()
    out = h.executor.execute(run, op_id, call)
    assert out.result_code == "proposal_rejected"
    assert out.status == "approved"
    assert not out.verified
    assert h.tool_calls("book") == []
    assert h.tool_calls("cancel") == []
    assert h.bookings_for(snap.hold_id) == h.bookings_for(other.hold_id) == 0
    assert h.op(op_id)["dispatch_count"] == 0
    # A later, exactly matching proposal is a separate step and executes normally.
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    assert h.executor.execute(run, op_id, book_call(snap)).status == "succeeded"


@pytest.mark.invariant
def test_supplied_key_matching_the_operation_is_accepted(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    key = h.op(op_id)["idempotency_key"]
    assert h.executor.execute(h.run(), op_id, book_call(snap, idempotency_key=key)).status == "succeeded"


@pytest.mark.invariant
def test_unknown_operation_cannot_be_executed(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    with pytest.raises(OperationNotFoundError):
        h.executor.execute(h.run(), uuid4(), book_call(snap))
    assert h.tool_calls("book") == []


@pytest.mark.invariant
def test_resume_never_dispatches_an_unvalidated_operation(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    out = h.executor.resume(h.run(), op_id)
    assert out.result_code == "awaiting_valid_proposal"
    assert h.tool_calls("book") == []


# ------------------------------------------------------------------ forced verification (E10/E11)
@pytest.mark.invariant
def test_success_is_reported_only_after_authoritative_status(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "succeeded"
    assert out.verified
    assert out.booking_id
    assert f"₹{snap.price_inr:,}" in out.message
    assert out.booking_id in out.message
    with h.db.txn() as conn:
        kinds = [
            (r["kind"], r["payload"].get("tool"))
            for r in conn.execute(
                "SELECT kind, payload FROM trace_events WHERE operation_id = %s ORDER BY id", (op_id,)
            ).fetchall()
        ]
    book_i = kinds.index(("tool.call", "book"))
    status_i = kinds.index(("tool.call", "get_booking_status"))
    finish_i = kinds.index(("operation.finished", None))
    assert book_i < status_i < finish_i
    assert h.session_state() == "completed"


@pytest.mark.invariant
def test_timeout_after_success_verifies_and_never_rebooks(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"]))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "succeeded"
    assert out.verified
    books = h.tool_calls("book")
    assert len(books) == 1
    assert books[0]["error"]["code"] == "timeout"
    statuses = h.tool_calls("get_booking_status")
    assert len(statuses) == 1
    assert statuses[0]["result"]["status"] == "booked"
    assert h.bookings_for(snap.hold_id) == 1


@pytest.mark.invariant
def test_timeout_without_booking_checks_status_then_retries_same_key(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_lost", "success"]))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "succeeded"
    books = h.tool_calls("book")
    statuses = h.tool_calls("get_booking_status")
    assert len(books) == 2
    assert len(statuses) == 2
    assert statuses[0]["result"]["status"] == "not_booked"
    keys = {b["args"]["idempotency_key"] for b in books}
    assert keys == {h.op(op_id)["idempotency_key"]}
    assert len({b["attempt_id"] for b in books}) == 2  # distinct attempts, one stable key
    assert h.bookings_for(snap.hold_id) == 1
    assert h.op(op_id)["dispatch_count"] == 2


@pytest.mark.invariant
def test_no_retry_once_the_hold_has_expired(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_lost", "success"]))
    # Approved with seconds to spare; the hold lapses while the timed-out call is in flight.
    h.clock.advance(HOLD_TTL - timedelta(seconds=3))
    h.executor.fault = advance_at(h.clock, "after_dispatch", timedelta(seconds=5))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "failed"
    assert out.result_code == "hold_expired"
    assert out.verified
    assert "haven't booked" in out.message
    assert len(h.tool_calls("book")) == 1
    assert h.bookings_for(snap.hold_id) == 0


@pytest.mark.invariant
def test_slot_taken_is_terminal_and_old_approval_cannot_rebook(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    proposal = h.gate.propose(h.ctx.session_id, snap)
    receipt = h.gate.display(h.ctx.session_id, proposal.id)
    op_id = h.gate.confirm(h.ctx.session_id, DEMO_USER, "yes", receipt.id).operation_id
    assert op_id is not None
    h.backend.configure(ScenarioConfig(book_outcomes=["slot_taken"]))
    run = h.run()
    out = h.executor.execute(run, op_id, book_call(snap))
    assert out.status == "failed"
    assert out.result_code == "slot_taken"
    assert "Nothing was booked" in out.message
    assert h.session_state() == "conflict"
    # Repeating the old confirmation returns the same finished operation; nothing re-executes.
    again = h.gate.confirm(h.ctx.session_id, DEMO_USER, "yes", receipt.id)
    assert again.outcome == "duplicate"
    assert again.operation_id == op_id
    assert h.executor.execute(run, op_id, book_call(snap)) == out
    assert len(h.tool_calls("book")) == 1
    # A replacement requires a brand-new proposal and approval.
    replacement = h.gate.propose(h.ctx.session_id, book_snapshot(h.tools, h.ctx, nth_slot=1))
    assert replacement.revision == proposal.revision + 1


@pytest.mark.invariant
def test_dispatch_lifetime_cap_is_three_and_ends_verified_not_booked(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_lost"] * 4))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "failed"
    assert out.result_code == "retries_exhausted"
    assert out.verified
    assert "Nothing was booked" in out.message
    assert len(h.tool_calls("book")) == 3
    assert h.op(op_id)["dispatch_count"] == 3


# ------------------------------------------------------------------ E23: run caps, deadlines, restarts
@pytest.mark.invariant
def test_run_tool_cap_stops_with_unknown_kept_unknown_and_new_run_cannot_reset_lifetime(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_lost"] * 4))
    first = h.executor.execute(h.run(RunLimits(tool_invocations=3)), op_id, book_call(snap))
    # book, status, book (timeout) -> the forced status check has no budget left.
    assert first.status == "pending_verification"
    assert not first.verified
    assert "not reporting it as done or as failed" in first.message
    assert len(h.tool_calls("book")) == 2
    # A fresh run continues the same operation; lifetime dispatches are not reset.
    second = h.executor.resume(h.run(), op_id)
    assert second.status == "failed"
    assert second.result_code == "retries_exhausted"
    assert len(h.tool_calls("book")) == 3
    assert h.bookings_for(snap.hold_id) == 0


@pytest.mark.invariant
def test_active_time_deadline_never_claims_unverified_success(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    h.executor.fault = advance_at(h.clock, "after_dispatch", timedelta(seconds=301))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "pending_verification"
    assert out.result_code == "limit_active_time"
    assert not out.verified
    assert "Booked" not in out.message
    assert h.bookings_for(snap.hold_id) == 1  # the backend did book; we just haven't verified it


@pytest.mark.invariant
def test_status_service_down_exhausts_reconciliations_and_stays_pending(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"], status_failures=50))
    out = h.executor.execute(h.run(), op_id, book_call(snap))
    assert out.status == "pending_verification"
    assert not out.verified
    op = h.op(op_id)
    assert op["reconcile_count"] == 6
    assert op["dispatch_count"] == 1  # never retried blind
    assert h.session_state() == "pending_verification"


@pytest.mark.invariant
def test_zero_tool_budget_sends_nothing_and_says_so(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    out = h.executor.execute(h.run(RunLimits(tool_invocations=0)), op_id, book_call(snap))
    assert out.status == "failed"
    assert out.result_code == "limit_tool_invocations"
    assert "Nothing was sent" in out.message
    assert h.tool_calls("book") == []


# ------------------------------------------------------------------ cancel and reschedule
@pytest.mark.invariant
def test_cancel_503_schedules_same_key_retry_then_verifies(h: Harness) -> None:
    op_id = h.approve(cancel_snapshot(h.db))
    run = h.run()
    first = h.executor.execute(run, op_id, ProposedCall(tool="cancel", arguments={"booking_id": AC_BOOKING}))
    assert first.status == "retry_wait"
    assert first.result_code == "retry_scheduled"
    op = h.op(op_id)
    assert op["next_retry_at"] == h.clock.now() + timedelta(seconds=1)
    assert count(h.db, "SELECT count(*) FROM jobs WHERE operation_id = %s AND status = 'pending'", (op_id,)) == 1
    assert h.session_state() == "retry_wait"
    # Not yet due: nothing is sent.
    assert h.executor.resume(run, op_id).result_code == "retry_scheduled"
    assert len(h.tool_calls("cancel")) == 1
    h.clock.advance(timedelta(seconds=1))
    done = h.executor.resume(run, op_id)
    assert done.status == "succeeded"
    assert done.verified
    cancels = h.tool_calls("cancel")
    assert len(cancels) == 2
    assert {c["args"]["idempotency_key"] for c in cancels} == {op["idempotency_key"]}
    assert len(h.tool_calls("list_bookings")) == 1  # authoritative verification


@pytest.mark.invariant
def test_reschedule_success_is_verified_against_history(h: Harness) -> None:
    snap = reschedule_snapshot(h.tools, h.ctx, h.db)
    op_id = h.approve(snap)
    out = h.executor.execute(
        h.run(),
        op_id,
        ProposedCall(tool="reschedule", arguments={"booking_id": AC_BOOKING, "slot": snap.slot.isoformat()}),
    )
    assert out.status == "succeeded"
    assert out.verified
    assert "moved from" in out.message


@pytest.mark.invariant
def test_reschedule_conflict_preserves_booking_and_reports_it(h: Harness) -> None:
    snap = reschedule_snapshot(h.tools, h.ctx, h.db)
    h.backend.configure(ScenarioConfig(reschedule_conflict_slots=[snap.slot]))
    op_id = h.approve(snap)
    out = h.executor.execute(
        h.run(),
        op_id,
        ProposedCall(tool="reschedule", arguments={"booking_id": AC_BOOKING, "slot": snap.slot.isoformat()}),
    )
    assert out.status == "failed"
    assert out.result_code == "slot_conflict"
    assert "unchanged" in out.message
    assert h.session_state() == "conflict"
    listed = h.tools.list_bookings(h.ctx, {"user_id": DEMO_USER})
    ac = next(b for b in listed.bookings if b.booking_id == AC_BOOKING)
    assert ac.slot == snap.old_slot
    assert ac.status == "confirmed"


@pytest.mark.invariant
def test_reschedule_proposal_with_a_different_slot_is_rejected(h: Harness) -> None:
    snap = reschedule_snapshot(h.tools, h.ctx, h.db)
    op_id = h.approve(snap)
    other = (snap.slot + timedelta(days=7)).isoformat()
    out = h.executor.execute(
        h.run(), op_id, ProposedCall(tool="reschedule", arguments={"booking_id": AC_BOOKING, "slot": other})
    )
    assert out.result_code == "proposal_rejected"
    assert h.tool_calls("reschedule") == []


def test_dispatch_trace_carries_attempt_and_generation(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    run = h.run()
    h.executor.execute(run, op_id, book_call(snap))
    (call,) = h.tool_calls("book")
    assert call["attempt_id"]
    assert call["claim_generation"] == run.claim.generation
