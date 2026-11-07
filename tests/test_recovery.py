"""Stage 3c invariants (E06, E13, E32): crash points, restart recovery and worker races.

A "crash" raises from a fault hook; a "restart" is a fresh Database pool, backend, ToolClient and
executor over the same Postgres, taking over after the dead worker's lease expires. Every test
asserts exactly one effect (or none) and that the reported outcome is verified.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.errors import SessionBusyError, StaleClaimError
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workflow.approval import ApprovalGate
from errand_agent.workflow.budget import RunManager
from errand_agent.workflow.executor import OperationExecutor, ProposedCall
from errand_agent.workflow.recovery import RecoveryService
from tests.conftest import make_session
from tests.helpers import count
from tests.test_executor import Harness, book_call, make_harness
from tests.workflow_helpers import AC_BOOKING, book_snapshot, cancel_snapshot

LEASE = 60


class SimulatedCrashError(Exception):
    pass


def crash_at(point: str) -> Callable[..., None]:
    def hook(at: str, *_: object) -> None:
        if at == point:
            raise SimulatedCrashError(point)

    return hook


@dataclass
class Process:
    """One worker process: its own pool, backend, tool client, executor and recovery service."""

    db: Database
    tools: ToolClient
    executor: OperationExecutor
    recovery: RecoveryService


@pytest.fixture
def h(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext) -> Harness:
    return make_harness(db, clock, backend, tools, ctx, lease=LEASE)


@pytest.fixture
def restart(test_conninfo: str, clock: FakeClock) -> Iterator[Callable[[], Process]]:
    opened: list[Database] = []

    def start() -> Process:
        pool = Database(test_conninfo)
        opened.append(pool)
        tools = ToolClient(MockBackend(pool, clock, seed=42), pool, clock)
        executor = OperationExecutor(pool, clock, tools)
        claims = ClaimManager(pool, clock, lease_seconds=LEASE)
        return Process(pool, tools, executor, RecoveryService(pool, clock, executor, claims, RunManager(pool, clock)))

    yield start
    for pool in opened:
        pool.close()


def _new_process(restart: Callable[[], Process]) -> Process:
    return restart()


def _approved_booking(h: Harness, outcome: str) -> tuple[UUID, ProposedCall, str]:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=[outcome, "success"]))
    return op_id, book_call(snap), snap.hold_id


# ------------------------------------------------------------------ crash points (E13)
@pytest.mark.invariant
@pytest.mark.parametrize(
    ("where", "point", "outcome", "traced_books", "started_books", "attempts"),
    [
        # Attempt reserved, tool never called: status says not booked, then one same-key dispatch.
        ("executor", "after_reserve", "success", 1, 1, 2),
        # Backend committed, client died before the reply: no tool.call event, but the write-ahead
        # tool.attempt_started shows the interrupted call. Status says booked: no re-dispatch.
        ("client", "after_backend", "success", 0, 1, 1),
        # Timed out after booking, worker died before recording it: status says booked.
        ("executor", "after_dispatch", "timeout_booked", 1, 1, 1),
        # Timed out without booking, worker died before recording: verify, then same-key retry.
        ("executor", "after_dispatch", "timeout_lost", 2, 2, 2),
    ],
)
def test_restart_after_crash_reconciles_before_retry_and_books_once(
    h: Harness,
    restart: Callable[[], Process],
    where: str,
    point: str,
    outcome: str,
    traced_books: int,
    started_books: int,
    attempts: int,
) -> None:
    op_id, call, hold_id = _approved_booking(h, outcome)
    if where == "executor":
        h.executor.fault = crash_at(point)
    else:
        h.tools.fault = crash_at(point)
    with pytest.raises(SimulatedCrashError):
        h.executor.execute(h.run(), op_id, call)
    assert h.session_state() == "executing"

    # The dead worker's lease runs out; a new process takes over and recovers.
    h.clock.advance(timedelta(seconds=LEASE + 1))
    proc = _new_process(restart)
    (out,) = proc.recovery.recover_session(h.ctx.session_id, "worker-2")
    assert out.status == "succeeded"
    assert out.verified
    assert h.bookings_for(hold_id) == 1
    books = h.tool_calls("book")
    assert len(books) == traced_books
    with h.db.txn() as conn:
        started = conn.execute(
            "SELECT count(*) AS n FROM trace_events WHERE kind = 'tool.attempt_started'"
            " AND payload->>'tool' = 'book' AND operation_id = %s",
            (op_id,),
        ).fetchone()
    assert started is not None
    assert started["n"] == started_books  # every call that reached the tool is visible, crashed or not
    assert {b["args"]["idempotency_key"] for b in books} <= {h.op(op_id)["idempotency_key"]}
    dispatches = "SELECT count(*) FROM operation_attempts WHERE operation_id = %s AND kind = 'dispatch'"
    assert count(h.db, dispatches, (op_id,)) == attempts
    # The new worker (generation 2) checked the source of truth before any dispatch of its own.
    with h.db.txn() as conn:
        events = [
            (r["payload"]["tool"], r["payload"].get("claim_generation"))
            for r in conn.execute(
                "SELECT payload FROM trace_events WHERE kind = 'tool.call' AND operation_id = %s ORDER BY id", (op_id,)
            ).fetchall()
        ]
    first_status = next(i for i, (tool, _) in enumerate(events) if tool == "get_booking_status")
    new_books = [i for i, (tool, gen) in enumerate(events) if tool == "book" and gen == 2]
    assert all(first_status < i for i in new_books)


@pytest.mark.invariant
def test_crash_after_backend_503_resumes_cancel_with_same_key(h: Harness, restart: Callable[[], Process]) -> None:
    op_id = h.approve(cancel_snapshot(h.db))
    h.tools.fault = crash_at("after_backend")
    # The backend's 503 is raised before after_backend fires; the crash happens on the *success* path.
    out = h.executor.execute(h.run(), op_id, ProposedCall(tool="cancel", arguments={"booking_id": AC_BOOKING}))
    assert out.status == "retry_wait"
    h.clock.advance(timedelta(seconds=LEASE + 1))
    with pytest.raises(SimulatedCrashError):
        h.executor.resume(h.run(holder="worker-1b"), op_id)  # retry reaches the backend, client dies
    h.clock.advance(timedelta(seconds=LEASE + 1))
    (done,) = _new_process(restart).recovery.recover_session(h.ctx.session_id, "worker-2")
    assert done.status == "succeeded"
    cancels = h.tool_calls("cancel")
    assert len({c["args"]["idempotency_key"] for c in cancels}) == 1
    assert count(h.db, "SELECT count(*) FROM mock_bookings WHERE id = %s AND status = 'cancelled'", (AC_BOOKING,)) == 1


# ------------------------------------------------------------------ repeated confirmation after restart (E06)
@pytest.mark.invariant
def test_repeat_confirmation_and_execution_after_restart_replays_the_same_effect(
    h: Harness, restart: Callable[[], Process], clock: FakeClock
) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    proposal = h.gate.propose(h.ctx.session_id, snap)
    receipt = h.gate.display(h.ctx.session_id, proposal.id)
    op_id = h.gate.confirm(h.ctx.session_id, DEMO_USER, "yes", receipt.id).operation_id
    assert op_id is not None
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    first = h.executor.execute(h.run(), op_id, book_call(snap))
    proc = _new_process(restart)
    again = ApprovalGate(proc.db, clock).confirm(h.ctx.session_id, DEMO_USER, f"confirm {proposal.token}", receipt.id)
    assert again.outcome == "duplicate"
    assert again.operation_id == op_id
    clock.advance(timedelta(seconds=LEASE + 1))
    claim = ClaimManager(proc.db, clock, lease_seconds=LEASE).acquire(h.ctx.session_id, "worker-2")
    replay = proc.executor.execute(RunManager(proc.db, clock).start(claim), op_id, book_call(snap))
    assert replay == first
    assert len(h.tool_calls("book")) == 1
    assert h.bookings_for(snap.hold_id) == 1


@pytest.mark.invariant
def test_restart_before_any_dispatch_does_not_execute_without_a_valid_proposal(
    h: Harness, restart: Callable[[], Process]
) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    proc = _new_process(restart)
    assert proc.recovery.unresolved_operations(h.ctx.session_id) == []  # 'approved' is not auto-resumed
    claim = ClaimManager(proc.db, h.clock, lease_seconds=LEASE).acquire(h.ctx.session_id, "worker-2")
    run = RunManager(proc.db, h.clock).start(claim)
    assert proc.executor.resume(run, op_id).result_code == "awaiting_valid_proposal"
    assert h.tool_calls("book") == []
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    assert proc.executor.execute(run, op_id, book_call(snap)).status == "succeeded"


# ------------------------------------------------------------------ worker races (E32)
@pytest.mark.invariant
def test_live_claim_blocks_a_second_worker(h: Harness, restart: Callable[[], Process]) -> None:
    op_id, _call, _hold = _approved_booking(h, "success")
    h.run()  # worker-1 holds a live lease
    with pytest.raises(SessionBusyError):
        _new_process(restart).recovery.recover_session(h.ctx.session_id, "worker-2")
    assert h.tool_calls("book") == []
    assert h.op(op_id)["dispatch_count"] == 0


@pytest.mark.invariant
@pytest.mark.parametrize("pause_at", ["after_reserve", "after_dispatch"])
def test_delayed_worker_is_fenced_and_cannot_duplicate_or_overwrite(
    h: Harness, restart: Callable[[], Process], pause_at: str
) -> None:
    """Worker A stalls past its lease; B takes over and finishes; A's late call/result is rejected."""
    op_id, call, hold_id = _approved_booking(h, "success")
    paused, resume = threading.Event(), threading.Event()

    def stall(at: str, _op: UUID) -> None:
        if at == pause_at:
            paused.set()
            resume.wait(10)

    h.executor.fault = stall
    errors: list[BaseException] = []
    run_a = h.run(holder="worker-a")

    def worker_a() -> None:
        try:
            h.executor.execute(run_a, op_id, call)
        except BaseException as exc:  # noqa: BLE001 - captured for assertion in the main thread
            errors.append(exc)

    thread = threading.Thread(target=worker_a)
    thread.start()
    assert paused.wait(10)
    h.clock.advance(timedelta(seconds=LEASE + 1))
    (out_b,) = _new_process(restart).recovery.recover_session(h.ctx.session_id, "worker-b")
    resume.set()
    thread.join(10)

    assert out_b.status == "succeeded"
    assert len(errors) == 1
    assert isinstance(errors[0], StaleClaimError)
    assert h.bookings_for(hold_id) == 1
    assert h.op(op_id)["status"] == "succeeded"  # A's late result did not overwrite B's verified outcome
    assert h.op(op_id)["result_code"] == "booked"


@pytest.mark.invariant
def test_unrelated_sessions_book_concurrently(
    db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient
) -> None:
    sessions = [make_session(db, DEMO_USER, clock.now()) for _ in range(3)]
    harnesses = [
        make_harness(db, clock, backend, tools, ToolContext(user_id=DEMO_USER, session_id=s)) for s in sessions
    ]
    work = []
    for i, hh in enumerate(harnesses):
        snap = book_snapshot(hh.tools, hh.ctx, nth_slot=i)
        work.append((hh, hh.approve(snap), book_call(snap)))
    backend.configure(ScenarioConfig(book_outcomes=["success"] * 3))
    results: list[str] = []
    barrier = threading.Barrier(3)

    def go(hh: Harness, op_id: UUID, call: ProposedCall) -> None:
        barrier.wait()
        results.append(hh.executor.execute(hh.run(), op_id, call).status)

    threads = [threading.Thread(target=go, args=w) for w in work]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results == ["succeeded"] * 3
    assert count(db, "SELECT count(*) FROM mock_bookings WHERE user_id = %s AND hold_id IS NOT NULL", (DEMO_USER,)) == 3
