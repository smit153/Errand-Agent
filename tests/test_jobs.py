"""Stage 6a: the background job worker and the outbox (E14, E31, E32, E44 delivery).

Invariants:
- a due job runs once: claimed with SKIP LOCKED, a crashed worker's job is reclaimed only after
  its lease expires, and a worker that lost a job can't close it;
- retries reuse the operation's key and lifetime budget; a retry that hits another 503 hands
  over to its own continuation (BUG-007);
- a crash mid-dispatch is finished by a worker that reconciles first, never by re-asking the model;
- an unresolved outcome always has a queued follow-up until the lifetime allowance is spent;
- the user hears about background outcomes once (outbox key per operation and status), and
  delivery is at-least-once with stable IDs.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from uuid import UUID

import pytest

from errand_agent import queue
from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workers.jobs import LEASE, JobWorker
from errand_agent.workers.outbox import Outbox, notify
from errand_agent.workflow.budget import RunManager
from errand_agent.workflow.executor import ProposedCall
from tests.helpers import count
from tests.test_executor import Harness, book_call, make_harness
from tests.workflow_helpers import AC_BOOKING, book_snapshot, cancel_snapshot

pytestmark = pytest.mark.invariant


class CrashError(Exception):
    pass


@pytest.fixture
def h(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext) -> Harness:
    return make_harness(db, clock, backend, tools, ctx, lease=60)


def worker(h: Harness, holder: str = "worker-A") -> JobWorker:
    return JobWorker(
        h.db,
        h.clock,
        h.executor,
        holder=holder,
        claims=ClaimManager(h.db, h.clock, lease_seconds=60),
        runs=RunManager(h.db, h.clock),
    )


def outbox(h: Harness) -> Outbox:
    return Outbox(h.db, h.clock, h.gate)


def cancel_in_turn(h: Harness) -> UUID:
    """The user approves a cancel; the in-turn dispatch hits the required 503 and the turn ends."""
    op_id = h.approve(cancel_snapshot(h.db))
    run = h.run(holder="agent")
    out = h.executor.execute(run, op_id, ProposedCall(tool="cancel", arguments={"booking_id": AC_BOOKING}))
    RunManager(h.db, h.clock).end(run, "replied")
    h.claims.release(run.claim)
    assert out.status == "retry_wait"
    return op_id


def jobs(h: Harness, op_id: UUID) -> list[tuple[str, str]]:
    with h.db.txn() as conn:
        rows = conn.execute(
            "SELECT kind, status FROM jobs WHERE operation_id = %s ORDER BY created_at, kind", (op_id,)
        ).fetchall()
    return [(r["kind"], r["status"]) for r in rows]


# ------------------------------------------------------------------ E14: cancel 503 → background retry
def test_e14_worker_retries_the_cancel_when_due_and_tells_the_user_once(h: Harness) -> None:
    op_id = cancel_in_turn(h)
    key = h.op(op_id)["idempotency_key"]
    assert sorted(jobs(h, op_id)) == [("operation_execute", "done"), ("operation_retry", "pending")]
    w = worker(h)
    assert w.run_due() == []  # not due yet: nothing claimed, nothing sent
    assert len(h.tool_calls("cancel")) == 1

    h.clock.advance(timedelta(seconds=1))
    [result] = w.run_due()
    assert (result.kind, result.status) == ("operation_retry", "done")
    assert h.op(op_id)["status"] == "succeeded"
    assert {c["args"]["idempotency_key"] for c in h.tool_calls("cancel")} == {key}
    assert len(h.tool_calls("cancel")) == 2
    assert count(h.db, "SELECT count(*) FROM mock_bookings WHERE id = %s AND status = 'cancelled'", (AC_BOOKING,)) == 1
    assert w.run_due() == []

    box = outbox(h)
    first = box.fetch(DEMO_USER)
    assert len(first) == 1
    assert first[0].text.startswith("Update on your earlier request: ")
    assert "cancel" in first[0].text.lower()
    again = box.fetch(DEMO_USER)  # not acknowledged: delivered again with the same ID
    assert [n.id for n in again] == [first[0].id]
    assert box.ack(DEMO_USER, [first[0].id]) == 1
    assert box.fetch(DEMO_USER) == []


def test_bug007_a_second_503_hands_over_to_a_second_retry(h: Harness) -> None:
    h.backend.configure(ScenarioConfig(cancel_extra_503=1))
    op_id = cancel_in_turn(h)
    w = worker(h)
    h.clock.advance(timedelta(seconds=1))
    [first] = w.run_due()
    assert first.status == "done"
    op = h.op(op_id)
    assert op["status"] == "retry_wait"
    assert op["next_retry_at"] == h.clock.now() + timedelta(seconds=2)
    assert ("operation_retry", "pending") in jobs(h, op_id)  # the continuation exists
    h.clock.advance(timedelta(seconds=2))
    [second] = w.run_due()
    assert second.status == "done"
    assert h.op(op_id)["status"] == "succeeded"
    assert h.op(op_id)["dispatch_count"] == 3  # the lifetime maximum, never more
    assert (
        count(h.db, "SELECT count(*) FROM jobs WHERE operation_id = %s AND status IN ('pending', 'claimed')", (op_id,))
        == 0
    )


# ------------------------------------------------------------------ E31: crashes around the retry
def test_e31_crash_after_the_retry_succeeded_but_before_it_was_recorded(h: Harness) -> None:
    op_id = cancel_in_turn(h)
    w = worker(h)
    crashed = {"done": False}

    def crash_once(point: str, _op: UUID) -> None:
        if point == "after_dispatch" and not crashed["done"]:
            crashed["done"] = True
            raise CrashError("worker died after the backend cancelled")

    h.executor.fault = crash_once
    h.clock.advance(timedelta(seconds=1))
    [result] = w.run_due()
    assert result.status == "requeued"  # recorded as a failure and retried with backoff
    h.executor.fault = None
    h.clock.advance(timedelta(seconds=5))
    [again] = w.run_due()
    assert again.status == "done"
    op = h.op(op_id)
    assert op["status"] == "succeeded"
    assert op["dispatch_count"] == 2  # reconciled as done: no third cancel
    assert len(h.tool_calls("cancel")) == 2


def test_e31_crash_mid_dispatch_is_finished_by_a_worker_without_the_model(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["success"]))

    def crash(point: str, _op: UUID) -> None:
        if point == "after_reserve":
            raise CrashError("process died before the dispatch was sent")

    h.executor.fault = crash
    run = h.run(holder="agent")
    with pytest.raises(CrashError):
        h.executor.execute(run, op_id, book_call(snap))
    h.executor.fault = None
    assert jobs(h, op_id) == [("operation_execute", "claimed")]  # still owned by the dead turn
    w = worker(h)
    assert w.run_due() == []  # the dead turn's lease hasn't expired yet
    h.clock.advance(timedelta(seconds=400))
    [result] = w.run_due()
    assert (result.kind, result.status) == ("operation_execute", "done")
    assert h.op(op_id)["status"] == "succeeded"
    assert h.bookings_for(snap.hold_id) == 1
    assert len(h.tool_calls("get_booking_status")) >= 1  # reconciled before dispatching again
    [note] = outbox(h).fetch(DEMO_USER)
    assert "Booked and verified" in note.text


def test_an_unchecked_approval_is_never_dispatched_by_a_worker(h: Harness, db: Database) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    with db.txn() as conn:  # a job for an operation whose proposal never passed the snapshot check
        queue.enqueue(
            conn,
            kind="operation_execute",
            session_id=h.ctx.session_id,
            operation_id=op_id,
            due_at=h.clock.now(),
            now=h.clock.now(),
        )
    [result] = worker(h).run_due()
    assert result.status == "done"
    assert h.op(op_id)["status"] == "approved"
    assert h.tool_calls("book") == []


# ------------------------------------------------------------------ unresolved outcomes keep a follow-up
def test_pending_verification_is_followed_up_until_resolved(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"], status_failures=50))
    run = h.run(holder="agent")
    out = h.executor.execute(run, op_id, book_call(snap))
    RunManager(h.db, h.clock).end(run, "replied")
    h.claims.release(run.claim)
    assert out.status == "pending_verification"
    assert ("operation_reconcile", "pending") in jobs(h, op_id)
    w = worker(h)
    h.backend.configure(ScenarioConfig(status_failures=0))  # the status service recovers
    h.clock.advance(timedelta(seconds=30))
    [result] = w.run_due()
    assert result.status == "done"
    assert h.op(op_id)["status"] == "succeeded"
    assert h.bookings_for(snap.hold_id) == 1  # found by the status check, never booked twice
    [note] = outbox(h).fetch(DEMO_USER)
    assert "Booked and verified" in note.text


def test_follow_ups_stop_at_the_lifetime_limit_and_say_so(h: Harness) -> None:
    snap = book_snapshot(h.tools, h.ctx)
    op_id = h.approve(snap)
    h.backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"], status_failures=1000))
    run = h.run(holder="agent")
    h.executor.execute(run, op_id, book_call(snap))
    RunManager(h.db, h.clock).end(run, "replied")
    h.claims.release(run.claim)
    w = worker(h)
    for _ in range(10):
        h.clock.advance(timedelta(seconds=30))
        w.run_due()
    op = h.op(op_id)
    assert op["status"] == "pending_verification"  # unknown stays unknown
    assert op["reconcile_count"] <= 6
    assert op["dispatch_count"] == 1  # never retried without verification
    assert (
        count(h.db, "SELECT count(*) FROM jobs WHERE operation_id = %s AND status IN ('pending', 'claimed')", (op_id,))
        == 0
    )
    notes = outbox(h).fetch(DEMO_USER)
    assert len(notes) == 1
    assert "still can't confirm" in notes[0].text


# ------------------------------------------------------------------ E32: workers and sessions
def test_e32_concurrent_workers_never_run_the_same_job(h: Harness) -> None:
    op_id = cancel_in_turn(h)
    h.clock.advance(timedelta(seconds=1))
    workers = [worker(h, f"worker-{i}") for i in range(4)]
    barrier = threading.Barrier(len(workers))
    claimed: list[list[queue.Job]] = []
    lock = threading.Lock()

    def claim(w: JobWorker) -> None:
        barrier.wait()
        with h.db.txn() as conn:
            got = queue.claim_due(conn, holder=w.holder, now=h.clock.now(), lease=LEASE, limit=10)
        with lock:
            claimed.append(got)

    threads = [threading.Thread(target=claim, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(len(c) for c in claimed) == 1
    assert [j.operation_id for c in claimed for j in c] == [op_id]


def test_e32_a_busy_session_puts_the_job_back_without_using_an_attempt(h: Harness) -> None:
    op_id = cancel_in_turn(h)
    user_turn = h.claims.acquire(h.ctx.session_id, "agent")  # the user is mid-turn
    h.clock.advance(timedelta(seconds=1))
    w = worker(h)
    [busy] = w.run_due()
    assert busy.status == "requeued"
    assert h.op(op_id)["status"] == "retry_wait"
    with h.db.txn() as conn:
        row = conn.execute(
            "SELECT attempts, due_at FROM jobs WHERE operation_id = %s AND kind = 'operation_retry'", (op_id,)
        ).fetchone()
    assert row is not None
    assert row["attempts"] == 0
    h.claims.release(user_turn)
    h.clock.advance(timedelta(seconds=2))
    [done] = w.run_due()
    assert done.status == "done"
    assert h.op(op_id)["status"] == "succeeded"


def test_e32_a_worker_that_lost_its_job_cannot_close_it(h: Harness) -> None:
    op_id = cancel_in_turn(h)
    h.clock.advance(timedelta(seconds=1))
    a, b = worker(h, "worker-A"), worker(h, "worker-B")
    with h.db.txn() as conn:
        [stale] = queue.claim_due(conn, holder=a.holder, now=h.clock.now(), lease=LEASE, limit=1)
    h.clock.advance(LEASE + timedelta(seconds=1))  # A stalls past its lease; B takes the job over
    [taken] = b.run_due()
    assert taken.status == "done"
    late = a.run(stale)
    assert late.status == "lost"
    assert h.op(op_id)["dispatch_count"] == 2  # A's late run sent nothing


# ------------------------------------------------------------------ outbox delivery (E44 part)
def test_outbox_dedupes_by_event_key_and_only_the_owner_can_ack(h: Harness, db: Database) -> None:
    with db.txn() as conn:
        assert notify(
            conn,
            user_id=DEMO_USER,
            session_id=h.ctx.session_id,
            event_key="k1",
            kind="operation_outcome",
            text="hello",
            now=h.clock.now(),
        )
        assert not notify(
            conn,
            user_id=DEMO_USER,
            session_id=h.ctx.session_id,
            event_key="k1",
            kind="operation_outcome",
            text="hello again",
            now=h.clock.now(),
        )
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
    box = outbox(h)
    [n] = box.fetch(DEMO_USER)
    assert n.text == "hello"
    assert box.fetch("user-other") == []
    assert box.ack("user-other", [n.id]) == 0
    assert box.ack(DEMO_USER, [n.id]) == 1
    assert box.ack(DEMO_USER, [n.id]) == 0
