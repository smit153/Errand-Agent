"""Stage 2c (E41, backend parts of E14/E15/E31): cancel's durable first-call 503 and reschedule conflicts."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from errand_agent.claims import Claim, ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError, ToolTransientError
from tests.helpers import count, make_operation, reserve_attempt

AC_BOOKING = "BK-H0002"  # seeded upcoming AC servicing booking for the demo user


class Dispatcher:
    def __init__(self, db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> None:
        self.db, self.clock, self.tools, self.ctx = db, clock, tools, ctx
        self.claim: Claim = ClaimManager(db, clock).acquire(ctx.session_id, "worker-1")

    def operation(self, action: str, snapshot: dict[str, Any], key: str) -> uuid.UUID:
        return make_operation(
            self.db,
            session_id=self.ctx.session_id,
            user_id=DEMO_USER,
            action=action,
            snapshot=snapshot,
            key=key,
            now=self.clock.now(),
        )

    def ctx_for(self, op: uuid.UUID, tool: str) -> ToolContext:
        return self.ctx.model_copy(update={"fence": reserve_attempt(self.db, self.claim, op, tool, self.clock.now())})


@pytest.fixture
def disp(db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> Dispatcher:
    return Dispatcher(db, clock, tools, ctx)


def _booking_status(db: Database, booking_id: str) -> str:
    with db.txn() as conn:
        row = conn.execute("SELECT status FROM mock_bookings WHERE id = %s", (booking_id,)).fetchone()
    assert row is not None
    return str(row["status"])


def _booking_slot(db: Database, booking_id: str) -> datetime:
    with db.txn() as conn:
        row = conn.execute("SELECT slot FROM mock_bookings WHERE id = %s", (booking_id,)).fetchone()
    assert row is not None
    slot: datetime = row["slot"]
    return slot


def _open_slot_for_ac_provider(tools: ToolClient, ctx: ToolContext, db: Database) -> datetime:
    with db.txn() as conn:
        row = conn.execute("SELECT provider_id FROM mock_bookings WHERE id = %s", (AC_BOOKING,)).fetchone()
    assert row is not None
    out = tools.search_providers(
        ctx,
        {"service": "ac_servicing", "area": "indiranagar", "date_range": {"start": "2026-11-07", "end": "2026-11-14"}},
    )
    provider = next(p for p in out.providers if p.provider_id == row["provider_id"])
    original = _booking_slot(db, AC_BOOKING)
    return next(s for s in provider.available_slots if s != original)


# ------------------------------------------------------------------ cancel
@pytest.mark.invariant
def test_first_cancel_fails_503_without_effect_then_same_key_retry_succeeds(disp: Dispatcher, db: Database) -> None:
    op = disp.operation("cancel", {"booking_id": AC_BOOKING}, "key-cancel")
    args = {"booking_id": AC_BOOKING, "idempotency_key": "key-cancel"}
    with pytest.raises(ToolTransientError) as info:
        disp.tools.cancel(disp.ctx_for(op, "cancel"), args)
    assert info.value.status == 503
    assert info.value.effect == "none"
    assert _booking_status(db, AC_BOOKING) == "confirmed"
    assert disp.tools.cancel(disp.ctx_for(op, "cancel"), args).status == "cancelled"
    assert _booking_status(db, AC_BOOKING) == "cancelled"
    # Replay is stable and never re-runs the 503 phase.
    assert disp.tools.cancel(disp.ctx_for(op, "cancel"), args).status == "cancelled"
    listed = disp.tools.list_bookings(disp.ctx, {"user_id": DEMO_USER})
    assert next(b for b in listed.bookings if b.booking_id == AC_BOOKING).status == "cancelled"


@pytest.mark.invariant
def test_cancel_503_phase_survives_backend_restart(disp: Dispatcher, db: Database, clock: FakeClock) -> None:
    op = disp.operation("cancel", {"booking_id": AC_BOOKING}, "key-cancel-restart")
    args = {"booking_id": AC_BOOKING, "idempotency_key": "key-cancel-restart"}
    with pytest.raises(ToolTransientError):
        disp.tools.cancel(disp.ctx_for(op, "cancel"), args)
    disp.tools = ToolClient(MockBackend(db, clock, seed=42), db, clock)  # fresh process, same DB
    assert disp.tools.cancel(disp.ctx_for(op, "cancel"), args).status == "cancelled"


def test_configured_extra_transient_faults_then_success(disp: Dispatcher, backend: MockBackend, db: Database) -> None:
    backend.configure(ScenarioConfig(cancel_extra_503=2))
    op = disp.operation("cancel", {"booking_id": AC_BOOKING}, "key-cancel-extra")
    args = {"booking_id": AC_BOOKING, "idempotency_key": "key-cancel-extra"}
    for _ in range(3):
        with pytest.raises(ToolTransientError):
            disp.tools.cancel(disp.ctx_for(op, "cancel"), args)
    assert disp.tools.cancel(disp.ctx_for(op, "cancel"), args).status == "cancelled"
    assert count(db, "SELECT calls FROM mock_idempotency WHERE tool = 'cancel' AND key = 'key-cancel-extra'") == 4


def test_a_new_key_gets_its_own_first_503(disp: Dispatcher) -> None:
    for key in ("key-a", "key-b"):
        op = disp.operation("cancel", {"booking_id": AC_BOOKING}, key)
        with pytest.raises(ToolTransientError):
            disp.tools.cancel(disp.ctx_for(op, "cancel"), {"booking_id": AC_BOOKING, "idempotency_key": key})


@pytest.mark.invariant
def test_provider_cancellation_between_attempts_is_reported_as_observed(
    disp: Dispatcher, backend: MockBackend, db: Database
) -> None:
    op = disp.operation("cancel", {"booking_id": AC_BOOKING}, "key-race")
    args = {"booking_id": AC_BOOKING, "idempotency_key": "key-race"}
    with pytest.raises(ToolTransientError):
        disp.tools.cancel(disp.ctx_for(op, "cancel"), args)
    backend.simulate_provider_cancellation(AC_BOOKING)
    out = disp.tools.cancel(disp.ctx_for(op, "cancel"), args)
    assert out.status == "not_cancellable"
    assert out.reason == "cancelled_by_provider"
    assert _booking_status(db, AC_BOOKING) == "cancelled_by_provider"


def test_cancelling_a_completed_booking_is_not_cancellable(disp: Dispatcher) -> None:
    op = disp.operation("cancel", {"booking_id": "BK-H0001"}, "key-done")
    out = disp.tools.cancel(disp.ctx_for(op, "cancel"), {"booking_id": "BK-H0001", "idempotency_key": "key-done"})
    assert out.status == "not_cancellable"
    assert out.reason == "completed"


@pytest.mark.invariant
def test_cancel_requires_fence_and_matching_snapshot(disp: Dispatcher, db: Database) -> None:
    with pytest.raises(ToolRejectedError) as info:
        disp.tools.cancel(disp.ctx, {"booking_id": AC_BOOKING, "idempotency_key": "key-x"})
    assert info.value.code == "fence_required"
    op = disp.operation("cancel", {"booking_id": "BK-H0001"}, "key-y")
    with pytest.raises(ToolRejectedError) as info:
        disp.tools.cancel(disp.ctx_for(op, "cancel"), {"booking_id": AC_BOOKING, "idempotency_key": "key-y"})
    assert info.value.code == "snapshot_mismatch"
    assert count(db, "SELECT count(*) FROM mock_idempotency WHERE tool = 'cancel'") == 0
    assert _booking_status(db, AC_BOOKING) == "confirmed"


def test_cancel_unknown_booking_is_rejected(disp: Dispatcher) -> None:
    op = disp.operation("cancel", {"booking_id": "BK-999999"}, "key-unknown")
    with pytest.raises(ToolRejectedError) as info:
        disp.tools.cancel(disp.ctx_for(op, "cancel"), {"booking_id": "BK-999999", "idempotency_key": "key-unknown"})
    assert info.value.code == "not_found"


# ------------------------------------------------------------------ reschedule
@pytest.mark.invariant
def test_reschedule_moves_the_booking_once_and_replays(disp: Dispatcher, db: Database) -> None:
    new_slot = _open_slot_for_ac_provider(disp.tools, disp.ctx, db)
    old_slot = _booking_slot(db, AC_BOOKING)
    snapshot = {"booking_id": AC_BOOKING, "slot": new_slot.isoformat()}
    op = disp.operation("reschedule", snapshot, "key-resched")
    args = {"booking_id": AC_BOOKING, "slot": new_slot.isoformat(), "idempotency_key": "key-resched"}
    out = disp.tools.reschedule(disp.ctx_for(op, "reschedule"), args)
    assert out.status == "rescheduled"
    assert out.old_slot == old_slot
    assert out.new_slot == new_slot
    assert _booking_slot(db, AC_BOOKING) == new_slot
    again = disp.tools.reschedule(disp.ctx_for(op, "reschedule"), args)
    assert again == out


@pytest.mark.invariant
def test_configured_conflict_preserves_original_booking(disp: Dispatcher, backend: MockBackend, db: Database) -> None:
    new_slot = _open_slot_for_ac_provider(disp.tools, disp.ctx, db)
    old_slot = _booking_slot(db, AC_BOOKING)
    backend.configure(ScenarioConfig(reschedule_conflict_slots=[new_slot]))
    op = disp.operation("reschedule", {"booking_id": AC_BOOKING, "slot": new_slot.isoformat()}, "key-conflict")
    args = {"booking_id": AC_BOOKING, "slot": new_slot.isoformat(), "idempotency_key": "key-conflict"}
    out = disp.tools.reschedule(disp.ctx_for(op, "reschedule"), args)
    assert out.status == "slot_conflict"
    assert _booking_slot(db, AC_BOOKING) == old_slot
    assert _booking_status(db, AC_BOOKING) == "confirmed"
    assert disp.tools.reschedule(disp.ctx_for(op, "reschedule"), args).status == "slot_conflict"


def test_reschedule_to_off_schedule_or_past_slot_conflicts(disp: Dispatcher, db: Database, clock: FakeClock) -> None:
    for i, bad in enumerate((_booking_slot(db, AC_BOOKING) + timedelta(minutes=17), clock.now() - timedelta(days=1))):
        key = f"key-bad-{i}"
        op = disp.operation("reschedule", {"booking_id": AC_BOOKING, "slot": bad.isoformat()}, key)
        out = disp.tools.reschedule(
            disp.ctx_for(op, "reschedule"), {"booking_id": AC_BOOKING, "slot": bad.isoformat(), "idempotency_key": key}
        )
        assert out.status == "slot_conflict"


@pytest.mark.invariant
def test_reschedule_slot_must_match_the_approved_snapshot(disp: Dispatcher, db: Database) -> None:
    approved = _open_slot_for_ac_provider(disp.tools, disp.ctx, db)
    other = approved + timedelta(days=7)
    op = disp.operation("reschedule", {"booking_id": AC_BOOKING, "slot": approved.isoformat()}, "key-snap")
    with pytest.raises(ToolRejectedError) as info:
        disp.tools.reschedule(
            disp.ctx_for(op, "reschedule"),
            {"booking_id": AC_BOOKING, "slot": other.isoformat(), "idempotency_key": "key-snap"},
        )
    assert info.value.code == "snapshot_mismatch"
    assert _booking_slot(db, AC_BOOKING) != other
