"""The structured errand request and how validated extractions update it.

Only fields the user's latest message actually mentions change; everything else is preserved,
so a one-detail correction never loses unrelated context (PDF §3). Every expression goes through
the deterministic resolvers in `parsing.py`; anything they can't resolve becomes a clarification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand_agent.tools.models import BookingRecord, Service
from errand_agent.workflow.parsing import (
    Budget,
    Clarification,
    DateWindow,
    TimeWindow,
    resolve_budget,
    resolve_date,
    resolve_time,
)
from errand_agent.workflow.ranking import RankedOption

Intent = Literal["book", "reschedule", "cancel"]
Field_ = Literal["service", "area", "date", "time", "budget", "provider", "booking"]


class RequestFacts(BaseModel):
    """Facts supplied by the user (validated). Never contains model prose or tool free text."""

    model_config = ConfigDict(frozen=True)

    intent: Intent = "book"
    service: Service | None = None
    area: str | None = None
    date: DateWindow | None = None
    time: TimeWindow | None = None
    budget: Budget | None = None
    same_as_last_time: bool = False
    preferred_provider_id: str | None = None
    preferred_provider_name: str | None = None


class Workspace(BaseModel):
    """Code-owned working data for the current errand (options shown, selection, targets, menu)."""

    model_config = ConfigDict(frozen=True)

    options: list[RankedOption] = Field(default_factory=list)
    selected_option: int | None = None
    target_booking: BookingRecord | None = None
    seen_provider_names: list[str] = Field(default_factory=list)
    menu: list[str] | None = None  # degraded-mode choices, as action codes
    menu_text: str | None = None  # the user message the menu's "try again" re-runs


class Extraction(BaseModel):
    """Validated output of the `update_request` tool (expressions are verbatim user spans)."""

    model_config = ConfigDict(frozen=True)

    intent: Intent | None = None
    service: Service | None = None
    area: str | None = None
    date_expression: str | None = None
    time_expression: str | None = None
    budget_expression: str | None = None
    provider_reference: Literal["same_as_last_time"] | None = None
    option_number: int | None = None
    # Explicit memory (PDF §3): only accepted when the user's own message asks to remember.
    remember: list[Literal["area", "time", "provider"]] = Field(default_factory=list)
    remember_note: str | None = None


def apply_extraction(
    req: RequestFacts, ext: Extraction, now: datetime
) -> tuple[RequestFacts, list[Clarification], set[str]]:
    """Return the updated request, clarifications needed, and which fields changed."""
    update: dict[str, Any] = {}
    clarifications: list[Clarification] = []
    for name in ("intent", "service", "area"):
        value = getattr(ext, name)
        if value is not None and value != getattr(req, name):
            update[name] = value
    resolvers: list[tuple[str, str | None, Any]] = [
        ("date", ext.date_expression, lambda e: resolve_date(e, now)),
        ("time", ext.time_expression, lambda e: resolve_time(e, now)),
        ("budget", ext.budget_expression, resolve_budget),
    ]
    for name, expression, resolve in resolvers:
        if not expression:
            continue
        resolved = resolve(expression)
        if isinstance(resolved, Clarification):
            clarifications.append(resolved)
        elif resolved != getattr(req, name):
            update[name] = resolved
    if ext.provider_reference == "same_as_last_time" and not req.same_as_last_time:
        update.update(same_as_last_time=True, preferred_provider_id=None, preferred_provider_name=None)
    return req.model_copy(update=update), clarifications, set(update)


def missing_fields(req: RequestFacts) -> list[Field_]:
    """Details still needed before the next tool can run, in the order they're asked."""
    if req.intent == "cancel":
        return []
    if req.intent == "reschedule":
        return ["date"] if req.date is None else []
    missing: list[Field_] = []
    if req.service is None and not req.same_as_last_time:
        missing.append("service")
    if req.area is None and req.preferred_provider_id is None and not req.same_as_last_time:
        missing.append("area")
    if req.date is None:
        missing.append("date")
    if req.budget is None:
        missing.append("budget")
    return missing
