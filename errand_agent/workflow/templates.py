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
