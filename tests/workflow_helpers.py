"""Builders for real snapshots from the seeded mock backend."""

from __future__ import annotations

from datetime import datetime

from errand_agent.db import Database
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workflow.snapshots import BookSnapshot, CancelSnapshot, RescheduleSnapshot

WEEK = {"start": "2026-11-06", "end": "2026-11-12"}
AC_BOOKING = "BK-H0002"


def book_snapshot(tools: ToolClient, ctx: ToolContext, *, nth_slot: int = 0, provider_index: int = 0) -> BookSnapshot:
    """Search, pick a provider/slot, hold it, and return the snapshot a card would show."""
    out = tools.search_providers(ctx, {"service": "plumber", "area": "indiranagar", "date_range": WEEK})
    candidates = [p for p in out.providers if len(p.available_slots) > nth_slot]
    provider = candidates[provider_index]
    hold = tools.hold_slot(
        ctx, {"provider_id": provider.provider_id, "slot": provider.available_slots[nth_slot].isoformat()}
    )
    return BookSnapshot(
        provider_id=provider.provider_id,
        provider_name=provider.name,
        service="plumber",
        slot=hold.slot,
        price_inr=hold.quoted_price_inr,
        hold_id=hold.hold_id,
        hold_expires_at=hold.expires_at,
    )


def _ac_booking(db: Database) -> tuple[str, str, datetime]:
    with db.txn() as conn:
        row = conn.execute(
            "SELECT b.provider_id, p.name, b.slot FROM mock_bookings b JOIN mock_providers p ON p.id = b.provider_id"
            " WHERE b.id = %s",
            (AC_BOOKING,),
        ).fetchone()
    assert row is not None
    return row["provider_id"], row["name"], row["slot"]


def cancel_snapshot(db: Database) -> CancelSnapshot:
    provider_id, name, slot = _ac_booking(db)
    return CancelSnapshot(
        booking_id=AC_BOOKING, provider_id=provider_id, provider_name=name, service="ac_servicing", slot=slot
    )


def reschedule_snapshot(tools: ToolClient, ctx: ToolContext, db: Database) -> RescheduleSnapshot:
    provider_id, name, old_slot = _ac_booking(db)
    out = tools.search_providers(
        ctx,
        {"service": "ac_servicing", "area": "indiranagar", "date_range": {"start": "2026-11-07", "end": "2026-11-14"}},
    )
    provider = next(p for p in out.providers if p.provider_id == provider_id)
    new_slot = next(s for s in provider.available_slots if s != old_slot)
    return RescheduleSnapshot(
        booking_id=AC_BOOKING,
        provider_id=provider_id,
        provider_name=name,
        service="ac_servicing",
        old_slot=old_slot,
        slot=new_slot,
    )
