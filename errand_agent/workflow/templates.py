"""Code-owned user-facing text for consequential details and outcomes (guardrail G5).

Prices, slots, providers, statuses and approval cards are rendered only from validated state,
never from model prose.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from errand_agent.workflow.snapshots import BookSnapshot, CancelSnapshot, RescheduleSnapshot, Snapshot

LOCAL_TZ = ZoneInfo("Asia/Kolkata")


class RankingOptionLike(Protocol):
    @property
    def option_no(self) -> int: ...
    @property
    def provider_id(self) -> str: ...
    @property
    def provider_name(self) -> str: ...
    @property
    def slot(self) -> datetime: ...
    @property
    def price_inr(self) -> int: ...
    @property
    def rating(self) -> float: ...


class BookingLike(Protocol):
    @property
    def booking_id(self) -> str: ...
    @property
    def service(self) -> str: ...
    @property
    def provider_name(self) -> str: ...
    @property
    def slot(self) -> datetime: ...


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


# ---------------------------------------------------------------------- conversation (agent) templates
_FIELD_QUESTIONS = {
    "service": "what service you need (plumber, electrician, AC servicing, carpenter or cleaning)",
    "area": "which area you're in (Indiranagar, Koramangala, HSR Layout, Whitefield or Jayanagar)",
    "date": "which day or days suit you",
    "time": "what time of day suits you",
    "budget": "your budget (or say 'no budget')",
    "provider": "which provider you mean",
    "booking": "which booking you mean",
}


def ask_fields(fields: list[str]) -> str:
    parts = [_FIELD_QUESTIONS.get(f, f) for f in fields]
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"Could you tell me {joined}?"


def clarification_notes(reasons: list[str]) -> str:
    return " ".join(f"{r[0].upper()}{r[1:]}." for r in reasons)


def window_text(start: object, end: object) -> str:
    return f"{start:%a %d %b}" if start == end else f"{start:%a %d %b}–{end:%a %d %b}"


def comparison(options: Sequence[RankingOptionLike], heading: str) -> str:
    lines = [heading]
    for o in options:
        lines.append(
            f"  {o.option_no}. {o.provider_name} ({o.provider_id}) — {when(o.slot)} — "
            f"{money(o.price_inr)} — ★{o.rating:.1f}"
        )
    return "\n".join(lines)


def no_match(over_budget: int, no_slot: int, not_preferred: int) -> str:
    reasons = []
    if over_budget:
        reasons.append(f"{over_budget} over your budget")
    if no_slot:
        reasons.append(f"{no_slot} with no slot in your time window")
    if not_preferred:
        reasons.append(f"{not_preferred} not the provider you asked for")
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    return (
        f"I couldn't find an option that fits all your constraints{detail}. "
        "I haven't loosened anything: tell me if you'd like to change the budget, day or time."
    )


def quote_over_budget(quote_inr: int, budget_inr: int) -> str:
    return (
        f"The held quote is {money(quote_inr)}, above your budget of {money(budget_inr)}, so I haven't asked you to "
        "approve it. Would you like to raise the budget or look at other options?"
    )


def booking_choices(bookings: Sequence[BookingLike]) -> str:
    lines = ["I found more than one matching booking. Which one do you mean?"]
    for b in bookings:
        lines.append(f"  - {b.booking_id}: {service_label(b.service)} with {b.provider_name} on {when(b.slot)}")
    return "\n".join(lines)


def no_booking_found(service: str | None) -> str:
    what = f"{service_label(service)} " if service else ""
    return f"I couldn't find an upcoming {what}booking for you, so there's nothing to change."


def no_history(service: str | None) -> str:
    what = f"{service_label(service)} " if service else ""
    return (
        f"I couldn't find a previous {what}booking, so I don't know who you used last time. Which provider do you mean?"
    )


def several_previous_providers(names: list[str]) -> str:
    return "You've used more than one provider for this before (" + ", ".join(names) + "). Which one do you mean?"


def preferred_provider(name: str, provider_id: str) -> str:
    return f"Last time you used {name} ({provider_id}); I'll look for them again."


MODEL_FAILED = "I couldn't understand that reliably, so I haven't taken any action."
NOT_SENT = (
    "I couldn't prepare the approved request, so nothing has been sent. Your approval is kept; reply 1 to try again."
)


def menu(items: list[tuple[str, str]]) -> str:
    lines = [MODEL_FAILED, "You can reply with a number:"]
    lines += [f"  {i + 1}. {label}" for i, (_code, label) in enumerate(items)]
    lines.append("Or set a detail directly, e.g. 'area: Koramangala', 'date: Saturday', 'budget: 1500'.")
    return "\n".join(lines)


def request_summary(facts: dict[str, str]) -> str:
    if not facts:
        return "I don't have any details for this request yet."
    return "Here's what I have so far: " + "; ".join(f"{k}: {v}" for k, v in facts.items()) + "."


STOPPED = "Okay, I've cleared this request. Nothing was booked or changed."
BUSY_WITH_OPERATION = "I'm still finishing your earlier request, so I haven't started anything new yet."


def options_heading(req: object) -> str:
    """Heading for the comparison: the constraints code applied, rendered from validated facts."""
    parts: list[str] = []
    date = getattr(req, "date", None)
    if date is not None:
        parts.append(window_text(date.start, date.end))
    time_window = getattr(req, "time", None)
    if time_window is not None:
        parts.append(time_window.source)
    budget = getattr(req, "budget", None)
    if budget is not None:
        parts.append("no budget limit" if budget.max_inr is None else f"up to {money(budget.max_inr)}")
    provider = getattr(req, "preferred_provider_name", None)
    if provider:
        parts.append(f"with {provider}")
    service = getattr(req, "service", None)
    label = service_label(service) if service else "provider"
    return f"Best {label} options ({'; '.join(parts)}), cheapest first:"


def quote_differs(search_price: int, quote: int) -> str:
    return f"Note: the listed price was {money(search_price)}, but the held quote is {money(quote)}."


def no_reschedule_slot(provider_name: str, start: object, end: object) -> str:
    return f"{provider_name} has no open slot on {window_text(start, end)}, so your booking is unchanged. Another day?"


def direct_field_unreadable(field: str, value: str) -> str:
    return f"I couldn't read '{value}' as a {field}, so I haven't changed anything."


def turn_limit(limit: str) -> str:
    return f"I stopped because a safety limit was reached ({limit}). Nothing new was booked or changed."


def tool_failed(tool: str) -> str:
    return (
        f"The provider system had a problem ({tool}), so I couldn't finish that step. Nothing was booked or "
        "changed; please try again."
    )


KEEP_CARD = (
    "I've kept the details above. Reply 'yes' or the confirmation code to approve them, or tell me what to change."
)
CARD_INVALIDATED = "Got it — I've updated the request, so the earlier approval card no longer applies."
