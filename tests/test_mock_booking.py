"""Stage 2b (E41, backend parts of E08–E13): hold_slot, book, get_booking_status.

Invariant tests first: replay never re-samples or duplicates; fenced/stale callers have zero effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from errand_agent.claims import Claim, ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import HOLD_TTL, MockBackend, ScenarioConfig, book_outcome, uniform
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError, ToolTimeoutError, ToolTransientError
from errand_agent.tools.models import BookOutput, HoldSlotOutput
from tests.helpers import count, make_operation, reserve_attempt

WEEK = {"start": "2026-11-06", "end": "2026-11-12"}


def _first_slot(tools: ToolClient, ctx: ToolContext, service: str = "plumber") -> tuple[str, datetime, int]:
    out = tools.search_providers(ctx, {"service": service, "area": "indiranagar", "date_range": WEEK})
    p = next(p for p in out.providers if p.available_slots)
    return p.provider_id, p.available_slots[0], p.price_inr


def _hold(tools: ToolClient, ctx: ToolContext) -> HoldSlotOutput:
    pid, slot, _ = _first_slot(tools, ctx)
    return tools.hold_slot(ctx, {"provider_id": pid, "slot": slot.isoformat()})


class Booker:
    """Creates an operation for a hold and dispatches book() with a freshly reserved attempt."""

    def __init__(self, db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> None:
        self.db, self.clock, self.tools, self.ctx = db, clock, tools, ctx
        self.claim: Claim = ClaimManager(db, clock).acquire(ctx.session_id, "worker-1")

    def operation(self, hold_id: str, key: str) -> uuid.UUID:
        return make_operation(
            self.db,
            session_id=self.ctx.session_id,
            user_id=DEMO_USER,
            action="book",
            snapshot={"hold_id": hold_id},
            key=key,
            now=self.clock.now(),
        )

    def book(self, op: uuid.UUID, hold_id: str, key: str) -> BookOutput:
        fence = reserve_attempt(self.db, self.claim, op, "book", self.clock.now())
        ctx = self.ctx.model_copy(update={"fence": fence})
        return self.tools.book(ctx, {"hold_id": hold_id, "idempotency_key": key})


@pytest.fixture
def booker(db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> Booker:
    return Booker(db, clock, tools, ctx)


def _rng_counter(db: Database) -> int:
    return count(db, "SELECT counter FROM mock_rng WHERE stream = 'book'")


def _bookings_for_hold(db: Database, hold_id: str) -> int:
    return count(db, "SELECT count(*) FROM mock_bookings WHERE hold_id = %s", (hold_id,))


# ------------------------------------------------------------------ outcome distribution (PDF §2)
@pytest.mark.parametrize(
    ("u", "expected"),
    [
        (0.0, "success"),
        (0.6999, "success"),
        (0.70, "slot_taken"),
        (0.8499, "slot_taken"),
        (0.85, "timeout_booked"),
        (0.9249, "timeout_booked"),
        (0.925, "timeout_lost"),
        (0.9999, "timeout_lost"),
    ],
)
def test_book_outcome_bucket_boundaries(u: float, expected: str) -> None:
    assert book_outcome(u) == expected


def test_default_distribution_is_70_15_and_half_of_timeouts_booked() -> None:
    n = 20000
    counts = dict.fromkeys(("success", "slot_taken", "timeout_booked", "timeout_lost"), 0)
    for i in range(1, n + 1):
        counts[book_outcome(uniform(42, "book", i))] += 1
    assert abs(counts["success"] / n - 0.70) < 0.015
    assert abs(counts["slot_taken"] / n - 0.15) < 0.015
    timeouts = counts["timeout_booked"] + counts["timeout_lost"]
    assert abs(timeouts / n - 0.15) < 0.015
    assert abs(counts["timeout_booked"] / timeouts - 0.5) < 0.05


def test_rng_sequence_survives_backend_restart(
    db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext, booker: Booker
) -> None:
    expected = [book_outcome(uniform(42, "book", i)) for i in (1, 2)]
    seen: list[str] = []
    for i in range(2):
        if i == 1:  # "restart": a brand-new backend/client over the same database
            booker.tools = ToolClient(MockBackend(db, clock, seed=42), db, clock)
        hold = _hold(booker.tools, ctx)
        op = booker.operation(hold.hold_id, f"key-restart-{i}")
        try:
            out = booker.book(op, hold.hold_id, f"key-restart-{i}")
            seen.append("success" if out.status == "confirmed" else out.status)
        except ToolTimeoutError:
            status = booker.tools.get_booking_status(ctx, {"idempotency_key": f"key-restart-{i}"})
            seen.append("timeout_booked" if status.status == "booked" else "timeout_lost")
    assert seen == expected
    assert _rng_counter(db) == 2


# ------------------------------------------------------------------ hold_slot
def test_hold_lasts_ten_minutes_and_quotes_search_price(tools: ToolClient, ctx: ToolContext, clock: FakeClock) -> None:
    pid, slot, price = _first_slot(tools, ctx)
    hold = tools.hold_slot(ctx, {"provider_id": pid, "slot": slot.isoformat()})
    assert hold.expires_at == clock.now() + HOLD_TTL
    assert hold.quoted_price_inr == price
    assert hold.slot == slot


def test_held_quote_can_differ_from_search_price(tools: ToolClient, ctx: ToolContext, backend: MockBackend) -> None:
    pid, slot, price = _first_slot(tools, ctx)
    backend.configure(ScenarioConfig(quote_drift_inr={pid: 250}))
    hold = tools.hold_slot(ctx, {"provider_id": pid, "slot": slot.isoformat()})
    assert hold.quoted_price_inr == price + 250


def test_held_slot_is_hidden_from_search_and_cannot_be_double_held(tools: ToolClient, ctx: ToolContext) -> None:
    pid, slot, _ = _first_slot(tools, ctx)
    tools.hold_slot(ctx, {"provider_id": pid, "slot": slot.isoformat()})
    out = tools.search_providers(ctx, {"service": "plumber", "area": "indiranagar", "date_range": WEEK})
    assert slot not in next(p for p in out.providers if p.provider_id == pid).available_slots
    with pytest.raises(ToolRejectedError) as info:
        tools.hold_slot(ctx, {"provider_id": pid, "slot": slot.isoformat()})
    assert info.value.code == "slot_unavailable"


def test_hold_rejects_off_schedule_and_past_slots(tools: ToolClient, ctx: ToolContext, clock: FakeClock) -> None:
    pid, slot, _ = _first_slot(tools, ctx)
    for bad in (slot + timedelta(minutes=30), clock.now() - timedelta(days=1)):
        with pytest.raises(ToolRejectedError) as info:
            tools.hold_slot(ctx, {"provider_id": pid, "slot": bad.isoformat()})
        assert info.value.code == "slot_unavailable"


# ------------------------------------------------------------------ book: branches
@pytest.mark.invariant
def test_success_books_once_and_status_confirms_by_key_and_hold(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["success"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-success")
    out = booker.book(op, hold.hold_id, "key-success")
    assert out.status == "confirmed"
    by_key = tools.get_booking_status(ctx, {"idempotency_key": "key-success"})
    by_hold = tools.get_booking_status(ctx, {"hold_id": hold.hold_id})
    assert by_key.status == by_hold.status == "booked"
    assert by_key.booking_id == by_hold.booking_id == out.booking_id
    assert by_key.price_inr == hold.quoted_price_inr
    assert _bookings_for_hold(db, hold.hold_id) == 1


@pytest.mark.invariant
def test_timeout_after_success_is_booked_exactly_once(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-tb")
    with pytest.raises(ToolTimeoutError) as info:
        booker.book(op, hold.hold_id, "key-tb")
    assert info.value.effect == "unknown"
    assert tools.get_booking_status(ctx, {"idempotency_key": "key-tb"}).status == "booked"
    # A same-key retry replays the committed effect; it never books twice.
    replay = booker.book(op, hold.hold_id, "key-tb")
    assert replay.status == "confirmed"
    assert _bookings_for_hold(db, hold.hold_id) == 1


@pytest.mark.invariant
def test_timeout_without_booking_then_same_key_retry_books_once(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["timeout_lost", "success"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-tl")
    with pytest.raises(ToolTimeoutError):
        booker.book(op, hold.hold_id, "key-tl")
    assert tools.get_booking_status(ctx, {"idempotency_key": "key-tl"}).status == "not_booked"
    assert _bookings_for_hold(db, hold.hold_id) == 0
    assert booker.book(op, hold.hold_id, "key-tl").status == "confirmed"
    assert _bookings_for_hold(db, hold.hold_id) == 1


@pytest.mark.invariant
def test_slot_taken_is_terminal_and_replayed(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["slot_taken"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-st")
    assert booker.book(op, hold.hold_id, "key-st").status == "slot_taken"
    draws = _rng_counter(db)
    assert booker.book(op, hold.hold_id, "key-st").status == "slot_taken"
    assert _rng_counter(db) == draws  # replay never re-samples
    assert tools.get_booking_status(ctx, {"idempotency_key": "key-st"}).status == "slot_taken"
    assert _bookings_for_hold(db, hold.hold_id) == 0


@pytest.mark.invariant
def test_expired_hold_returns_hold_expired_without_booking(
    booker: Booker, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-exp")
    clock.advance(HOLD_TTL)
    booker.claim = ClaimManager(db, clock).acquire(ctx.session_id, "worker-1")  # lease renewed after the wait
    draws = _rng_counter(db)
    assert booker.book(op, hold.hold_id, "key-exp").status == "hold_expired"
    assert _rng_counter(db) == draws
    assert tools.get_booking_status(ctx, {"hold_id": hold.hold_id}).status == "hold_expired"
    assert _bookings_for_hold(db, hold.hold_id) == 0


@pytest.mark.invariant
def test_committed_replay_returns_same_booking_and_never_resamples(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["success"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-rep")
    first = booker.book(op, hold.hold_id, "key-rep")
    draws = _rng_counter(db)
    for _ in range(3):
        again = booker.book(op, hold.hold_id, "key-rep")
        assert again.booking_id == first.booking_id
    assert _rng_counter(db) == draws
    assert _bookings_for_hold(db, hold.hold_id) == 1
    assert count(db, "SELECT calls FROM mock_idempotency WHERE tool = 'book' AND key = 'key-rep'") == 4


# ------------------------------------------------------------------ book: fencing (zero effect)
def _assert_rejected_without_effect(
    exc: pytest.ExceptionInfo[ToolRejectedError], code: str, db: Database, hold_id: str, draws: int
) -> None:
    assert exc.value.code == code
    assert exc.value.effect == "none"
    assert _bookings_for_hold(db, hold_id) == 0
    assert _rng_counter(db) == draws


@pytest.mark.invariant
def test_book_without_fence_is_rejected(tools: ToolClient, ctx: ToolContext, db: Database) -> None:
    hold = _hold(tools, ctx)
    draws = _rng_counter(db)
    with pytest.raises(ToolRejectedError) as exc:
        tools.book(ctx, {"hold_id": hold.hold_id, "idempotency_key": "key-nofence"})
    _assert_rejected_without_effect(exc, "fence_required", db, hold.hold_id, draws)


@pytest.mark.invariant
def test_stale_generation_cannot_commit_an_effect(
    booker: Booker, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-stale")
    stale_fence = reserve_attempt(db, booker.claim, op, "book", clock.now())
    clock.advance(timedelta(seconds=61))
    ClaimManager(db, clock).acquire(ctx.session_id, "worker-2")  # takes over, fencing worker-1
    draws = _rng_counter(db)
    with pytest.raises(ToolRejectedError) as exc:
        tools.book(
            ctx.model_copy(update={"fence": stale_fence}), {"hold_id": hold.hold_id, "idempotency_key": "key-stale"}
        )
    _assert_rejected_without_effect(exc, "fenced", db, hold.hold_id, draws)


@pytest.mark.invariant
def test_attempt_must_be_reserved_and_unused(
    booker: Booker, backend: MockBackend, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    backend.configure(ScenarioConfig(book_outcomes=["timeout_lost"]))
    hold = _hold(tools, ctx)
    op = booker.operation(hold.hold_id, "key-att")
    fence = reserve_attempt(db, booker.claim, op, "book", clock.now())
    with db.txn() as conn:
        conn.execute("UPDATE operation_attempts SET status = 'completed' WHERE id = %s", (fence.attempt_id,))
    draws = _rng_counter(db)
    with pytest.raises(ToolRejectedError) as exc:
        tools.book(ctx.model_copy(update={"fence": fence}), {"hold_id": hold.hold_id, "idempotency_key": "key-att"})
    _assert_rejected_without_effect(exc, "fenced", db, hold.hold_id, draws)


@pytest.mark.invariant
@pytest.mark.parametrize("variant", ["other_hold", "other_key"])
def test_arguments_must_match_the_approved_snapshot(
    booker: Booker, tools: ToolClient, ctx: ToolContext, db: Database, variant: str
) -> None:
    approved = _hold(tools, ctx)
    other = _hold(tools, ctx)
    op = booker.operation(approved.hold_id, "key-snap")
    args = (
        {"hold_id": other.hold_id, "idempotency_key": "key-snap"}
        if variant == "other_hold"
        else {"hold_id": approved.hold_id, "idempotency_key": "key-not-the-operation-key"}
    )
    draws = _rng_counter(db)
    fence = reserve_attempt(db, booker.claim, op, "book", booker.clock.now())
    with pytest.raises(ToolRejectedError) as exc:
        tools.book(ctx.model_copy(update={"fence": fence}), args)
    _assert_rejected_without_effect(exc, "snapshot_mismatch", db, args["hold_id"], draws)


@pytest.mark.invariant
def test_foreign_hold_is_not_found(
    booker: Booker, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
    other_session = count(
        db,
        "INSERT INTO sessions (user_id, created_at, updated_at) VALUES ('user-other', %s, %s) RETURNING id",
        (clock.now(), clock.now()),
    )
    other_ctx = ToolContext(user_id="user-other", session_id=other_session)
    foreign = _hold(tools, other_ctx)
    with pytest.raises(ToolRejectedError) as info:
        tools.get_booking_status(ctx, {"hold_id": foreign.hold_id})
    assert info.value.code == "not_found"


# ------------------------------------------------------------------ status failures
def test_configured_status_failures_raise_transient_then_recover(
    backend: MockBackend, tools: ToolClient, ctx: ToolContext
) -> None:
    hold = _hold(tools, ctx)
    backend.configure(ScenarioConfig(status_failures=2))
    for _ in range(2):
        with pytest.raises(ToolTransientError):
            tools.get_booking_status(ctx, {"hold_id": hold.hold_id})
    assert tools.get_booking_status(ctx, {"hold_id": hold.hold_id}).status == "not_booked"


def test_status_requires_exactly_one_lookup_key(tools: ToolClient, ctx: ToolContext) -> None:
    for args in ({}, {"hold_id": "HLD-000001", "idempotency_key": "k"}):
        with pytest.raises(ToolRejectedError) as info:
            tools.get_booking_status(ctx, args)
        assert info.value.code == "invalid_arguments"
