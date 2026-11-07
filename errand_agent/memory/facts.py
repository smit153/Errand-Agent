"""Typed memory facts (PDF §3 persistent memory; DECISIONS §§7, 10).

Only typed fields can be stored: preferred area, earliest acceptable time, a preferred provider
for one service, and a short free-text note in the user's own words. Every value is validated
here. The canonical text derived from a value is what gets embedded; it is built by code, never
taken from the model.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from errand_agent.tools.models import Area, Service

MemoryField = Literal["area", "earliest_time", "preferred_provider", "note"]
Scope = Literal["global", "plumber", "electrician", "ac_servicing", "carpenter", "cleaning"]
Origin = Literal["explicit", "inferred"]
Status = Literal["active", "unconfirmed", "pending_embedding", "superseded", "forgotten"]

NOTE_MAX_CHARS = 200
_PROVIDER_ID = r"^[A-Z]{3}-[A-Z]{3}-\d{2}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AreaValue(_Frozen):
    kind: Literal["area"] = "area"
    area: Area


class EarliestTimeValue(_Frozen):
    kind: Literal["earliest_time"] = "earliest_time"
    earliest: time

    @model_validator(mode="after")
    def _whole_minutes(self) -> EarliestTimeValue:
        if self.earliest.second or self.earliest.microsecond or self.earliest.tzinfo is not None:
            raise ValueError("earliest time must be a naive local time in whole minutes")
        return self


class ProviderValue(_Frozen):
    kind: Literal["preferred_provider"] = "preferred_provider"
    provider_id: Annotated[str, StringConstraints(pattern=_PROVIDER_ID)]
    provider_name: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class NoteValue(_Frozen):
    kind: Literal["note"] = "note"
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NOTE_MAX_CHARS)]


FactValue = Annotated[AreaValue | EarliestTimeValue | ProviderValue | NoteValue, Field(discriminator="kind")]
FACT_VALUE: TypeAdapter[AreaValue | EarliestTimeValue | ProviderValue | NoteValue] = TypeAdapter(FactValue)


def check_scope(value: AreaValue | EarliestTimeValue | ProviderValue | NoteValue, scope: Scope) -> None:
    """Which scopes each field may have. A provider preference always belongs to one service."""
    if isinstance(value, AreaValue) and scope != "global":
        raise ValueError("a preferred area is global")
    if isinstance(value, ProviderValue) and scope == "global":
        raise ValueError("a preferred provider belongs to one service")


def _service_label(service: str) -> str:
    return {"ac_servicing": "AC servicing"}.get(service, service.replace("_", " "))


def _clock_text(t: time) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def canonical_text(value: AreaValue | EarliestTimeValue | ProviderValue | NoteValue, scope: Scope) -> str:
    """The code-built sentence that is embedded and shown to the user."""
    match value:
        case AreaValue():
            return f"Preferred area: {value.area.replace('_', ' ').title()}"
        case EarliestTimeValue():
            where = "" if scope == "global" else f" for {_service_label(scope)}"
            return f"Earliest acceptable appointment time{where}: {_clock_text(value.earliest)}"
        case ProviderValue():
            return f"Preferred {_service_label(scope)} provider: {value.provider_name} ({value.provider_id})"
        case NoteValue():
            where = "" if scope == "global" else f" for {_service_label(scope)}"
            return f"Note{where}: {value.text}"


def content_hash(model: str, document_text: str) -> str:
    return hashlib.sha256(f"{model}\n{document_text}".encode()).hexdigest()


class MemoryFact(_Frozen):
    """A stored fact as read back from the database. `value` is None only once forgotten."""

    id: UUID
    user_id: str
    display_no: int
    field: MemoryField
    scope: Scope
    value: FactValue | None
    origin: Origin
    status: Status
    confirmed: bool
    revision: int
    provenance: dict[str, object]
    created_at: datetime
    updated_at: datetime

    @property
    def text(self) -> str:
        if self.value is None:
            return "(forgotten)"
        return canonical_text(self.value, self.scope)

    @property
    def service(self) -> Service | None:
        return None if self.scope == "global" else self.scope
