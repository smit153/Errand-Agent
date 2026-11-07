"""Code-owned user-facing text for consequential details and outcomes (guardrail G5).

Prices, slots, providers, statuses and approval cards are rendered only from validated state,
never from model prose.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from errand_agent.workflow.snapshots import BookSnapshot, CancelSnapshot, RescheduleSnapshot, Snapshot

LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def money(amount_inr: int) -> str:
    return f"₹{amount_inr:,}"


def when(dt: datetime) -> str:
    local = dt.astimezone(LOCAL_TZ)
    hour12 = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{local:%a} {local.day} {local:%b}, {hour12}:{local.minute:02d} {suffix}"


def service_label(service: str) -> str:
    return {"ac_servicing": "AC servicing"}.get(service, service.replace("_", " "))


def approval_card(snapshot: Snapshot, token: str, revision: int) -> str:
    lines: list[str]
    match snapshot:
        case BookSnapshot():
            lines = [
                f"Please confirm this booking (revision {revision}):",
                f"  Service:  {service_label(snapshot.service)}",
                f"  Provider: {snapshot.provider_name} ({snapshot.provider_id})",
                f"  Slot:     {when(snapshot.slot)}",
                f"  Price:    {money(snapshot.price_inr)} (held quote)",
                f"  Hold expires: {when(snapshot.hold_expires_at)}",
            ]
        case CancelSnapshot():
            lines = [
                f"Please confirm this cancellation (revision {revision}):",
                f"  Booking:  {snapshot.booking_id} — {service_label(snapshot.service)}",
                f"  Provider: {snapshot.provider_name} ({snapshot.provider_id})",
                f"  Slot:     {when(snapshot.slot)}",
            ]
        case RescheduleSnapshot():
            lines = [
                f"Please confirm this reschedule (revision {revision}):",
                f"  Booking:  {snapshot.booking_id} — {service_label(snapshot.service)}",
                f"  Provider: {snapshot.provider_name} ({snapshot.provider_id})",
                f"  From:     {when(snapshot.old_slot)}",
                f"  To:       {when(snapshot.slot)}",
            ]
    lines.append(f"Reply 'yes' or 'confirm {token}' to approve exactly this. Anything else changes nothing.")
    return "\n".join(lines)


def material_change(old: Snapshot, new: Snapshot) -> str:
    """Explain the material difference between an approved-for revision and a newer one."""
    parts: list[str] = []
    if isinstance(old, BookSnapshot) and isinstance(new, BookSnapshot):
        if old.price_inr != new.price_inr:
            parts.append(f"The quote changed from {money(old.price_inr)} to {money(new.price_inr)}.")
        if old.provider_id != new.provider_id:
            parts.append(f"The provider changed from {old.provider_name} to {new.provider_name}.")
        if old.slot != new.slot:
            parts.append(f"The slot changed from {when(old.slot)} to {when(new.slot)}.")
        if not parts and old.hold_id != new.hold_id:
            parts.append("The slot hold was renewed.")
    elif isinstance(old, RescheduleSnapshot) and isinstance(new, RescheduleSnapshot) and old.slot != new.slot:
        parts.append(f"The new slot changed from {when(old.slot)} to {when(new.slot)}.")
    if not parts:
        parts.append("The details you were approving have changed.")
    parts.append("Your earlier approval no longer applies; please review the current details.")
    return " ".join(parts)


def hold_expired(slot: datetime) -> str:
    return (
        f"The hold for {when(slot)} expired, so I haven't booked it. I'll check availability again. "
        "Any new hold needs your approval."
    )


NO_CARD = "There's nothing waiting for your approval right now, so I haven't taken any action."
CODE_MISMATCH = "That confirmation code doesn't match the details I showed you, so I haven't taken any action."
NOT_OWNER = "That confirmation doesn't belong to this conversation, so I haven't taken any action."
INVALIDATED = "Those details are no longer current, so I haven't taken any action. Please review the latest details."


# ---------------------------------------------------------------------- execution outcomes
_ACTION_NOUN = {"book": "booking", "cancel": "cancellation", "reschedule": "reschedule"}

PROPOSAL_REJECTED = (
    "I didn't act on that: the proposed action didn't exactly match what you approved, so nothing was sent."
)
AWAITING_PROPOSAL = "Your approval is recorded, but I haven't sent anything yet."


def booking_confirmed(snap: BookSnapshot, booking_id: str) -> str:
    return (
        f"Booked and verified with the provider system: {snap.provider_name} for {service_label(snap.service)} "
        f"on {when(snap.slot)} at {money(snap.price_inr)}. Booking ID {booking_id}."
    )


def cancelled(snap: CancelSnapshot) -> str:
    return f"Cancelled and verified: booking {snap.booking_id} with {snap.provider_name} on {when(snap.slot)}."


def rescheduled(snap: RescheduleSnapshot) -> str:
    return (
        f"Rescheduled and verified: booking {snap.booking_id} with {snap.provider_name} moved from "
        f"{when(snap.old_slot)} to {when(snap.slot)}."
    )


def retry_scheduled(action: str) -> str:
    return (
        f"The provider system is temporarily unavailable, so the {_ACTION_NOUN.get(action, action)} hasn't gone "
        "through yet. I'll retry automatically with the same request, so it can't be applied twice."
    )


def pending_verification(action: str, limit: str | None = None) -> str:
    stop = f" I stopped because a safety limit was reached ({limit})." if limit else ""
    return (
        f"I couldn't confirm whether the {_ACTION_NOUN.get(action, action)} went through, so I'm not reporting it "
        f"as done or as failed.{stop} I won't repeat it until I've checked with the provider system."
    )


def limit_not_done(action: str, limit: str, *, dispatched: bool) -> str:
    sent = "I checked with the provider system and" if dispatched else "Nothing was sent, and"
    return (
        f"I stopped because a safety limit was reached ({limit}). {sent} the "
        f"{_ACTION_NOUN.get(action, action)} has not been made. Any new attempt needs your fresh approval."
    )


_REASONS = {
    "slot_taken": "someone else took that slot before it could be confirmed",
    "slot_conflict": "that new slot isn't available",
    "retries_exhausted": "the provider system kept failing after the allowed retries",
    "cancelled_by_provider": "the provider had already cancelled this booking",
    "completed": "the booking has already been completed",
    "cancelled": "the booking was already cancelled",
    "hold_not_active": "the slot hold is no longer active",
    "not_found": "the provider system doesn't recognise it",
}


def not_done(action: str, snap: Snapshot, code: str) -> str:
    reason = _REASONS.get(code, f"the provider system refused it ({code})")
    if action == "book":
        tail = "Nothing was booked. I can look for other options; any new booking needs your approval."
    elif action == "cancel":
        tail = f"Booking {getattr(snap, 'booking_id', '')} was not cancelled by me."
    else:
        old = when(snap.old_slot) if isinstance(snap, RescheduleSnapshot) else "its original time"
        tail = f"Your booking is unchanged at {old}. Any alternative needs your fresh approval."
    return f"I couldn't complete the {_ACTION_NOUN.get(action, action)}: {reason}. {tail}"
