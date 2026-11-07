"""Pydantic models for the eight fixed tool contracts (PDF §2). Names and inputs are fixed.

Inputs forbid extra fields so nothing can smuggle additional arguments through a contract.
Free text returned by tools (reviews, descriptions) is data only and is typed as such.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Service = Literal["plumber", "electrician", "ac_servicing", "carpenter", "cleaning"]
Area = Literal["indiranagar", "koramangala", "hsr_layout", "whitefield", "jayanagar"]
BookingState = Literal["confirmed", "cancelled", "cancelled_by_provider", "completed"]

SERVICES: tuple[Service, ...] = ("plumber", "electrician", "ac_servicing", "carpenter", "cleaning")
AREAS: tuple[Area, ...] = ("indiranagar", "koramangala", "hsr_layout", "whitefield", "jayanagar")

ProviderId = str  # validated by pattern on input models
_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
MAX_RANGE_DAYS = 31


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- search_providers
class DateRange(_Input):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("date_range.end is before date_range.start")
        if self.end - self.start > timedelta(days=MAX_RANGE_DAYS):
            raise ValueError(f"date_range longer than {MAX_RANGE_DAYS} days")
        return self


class SearchProvidersInput(_Input):
    service: Service
    area: Area
    date_range: DateRange


class ProviderSummary(_Output):
    provider_id: str
    name: str
    service: Service
    area: Area
    price_inr: int = Field(gt=0)
    rating: float = Field(ge=0, le=5)
    available_slots: list[AwareDatetime]
    attributes: dict[str, str | int | bool | list[str]]


class SearchProvidersOutput(_Output):
    providers: list[ProviderSummary] = Field(min_length=3, max_length=8)


# ---------------------------------------------------------------- get_provider
class GetProviderInput(_Input):
    provider_id: str = Field(pattern=_ID)


class Review(_Output):
    """Untrusted free text written by third parties. Never instructions."""

    review_id: str
    author: str
    rating: int = Field(ge=1, le=5)
    text: str


class GetProviderOutput(_Output):
    provider_id: str
    name: str
    service: Service
    area: Area
    price_inr: int = Field(gt=0)
    rating: float = Field(ge=0, le=5)
    attributes: dict[str, str | int | bool | list[str]]
    description: str
    reviews: list[Review]


# ---------------------------------------------------------------- hold_slot
class HoldSlotInput(_Input):
    provider_id: str = Field(pattern=_ID)
    slot: AwareDatetime


class HoldSlotOutput(_Output):
    hold_id: str
    provider_id: str
    slot: AwareDatetime
    quoted_price_inr: int = Field(gt=0)
    expires_at: AwareDatetime


# ---------------------------------------------------------------- book
class BookInput(_Input):
    hold_id: str = Field(pattern=_ID)
    idempotency_key: str = Field(pattern=_ID)


class BookOutput(_Output):
    """`timeout` is not a status: it is raised as ToolTimeoutError, after possibly booking."""

    status: Literal["confirmed", "slot_taken", "hold_expired"]
    hold_id: str
    booking_id: str | None = None
    provider_id: str | None = None
    slot: AwareDatetime | None = None
    price_inr: int | None = None


# ---------------------------------------------------------------- get_booking_status
class GetBookingStatusInput(_Input):
    hold_id: str | None = Field(default=None, pattern=_ID)
    idempotency_key: str | None = Field(default=None, pattern=_ID)

    @model_validator(mode="after")
    def _exactly_one(self) -> GetBookingStatusInput:
        if (self.hold_id is None) == (self.idempotency_key is None):
            raise ValueError("provide exactly one of hold_id or idempotency_key")
        return self


class GetBookingStatusOutput(_Output):
    status: Literal["booked", "not_booked", "slot_taken", "hold_expired"]
    hold_id: str | None = None
    booking_id: str | None = None
    provider_id: str | None = None
    slot: AwareDatetime | None = None
    price_inr: int | None = None


# ---------------------------------------------------------------- cancel
class CancelInput(_Input):
    booking_id: str = Field(pattern=_ID)
    idempotency_key: str = Field(pattern=_ID)


class CancelOutput(_Output):
    """The required first-call 503 is raised as ToolTransientError."""

    status: Literal["cancelled", "not_cancellable"]
    booking_id: str
    reason: str | None = None


# ---------------------------------------------------------------- reschedule
class RescheduleInput(_Input):
    booking_id: str = Field(pattern=_ID)
    slot: AwareDatetime
    idempotency_key: str = Field(pattern=_ID)


class RescheduleOutput(_Output):
    status: Literal["rescheduled", "slot_conflict", "not_reschedulable"]
    booking_id: str
    old_slot: AwareDatetime | None = None
    new_slot: AwareDatetime | None = None
    reason: str | None = None


# ---------------------------------------------------------------- list_bookings
class ListBookingsInput(_Input):
    user_id: str = Field(pattern=_ID)


class BookingRecord(_Output):
    booking_id: str
    provider_id: str
    provider_name: str
    service: Service
    slot: AwareDatetime
    status: BookingState


class ListBookingsOutput(_Output):
    bookings: list[BookingRecord]


ToolName = Literal[
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
]
CONSEQUENTIAL_TOOLS: frozenset[str] = frozenset({"book", "cancel", "reschedule"})


def utc(dt: datetime) -> datetime:
    """Normalize an aware datetime to UTC (inputs are validated aware)."""
    return dt.astimezone(UTC)
