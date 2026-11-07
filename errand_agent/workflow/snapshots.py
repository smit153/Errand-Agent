"""Immutable snapshots of consequential actions: exactly what the user approves and code dispatches."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter


class _Snap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BookSnapshot(_Snap):
    action: Literal["book"] = "book"
    provider_id: str
    provider_name: str
    service: str
    slot: AwareDatetime
    price_inr: int = Field(gt=0)
    hold_id: str
    hold_expires_at: AwareDatetime

    def dispatch_args(self, key: str) -> dict[str, Any]:
        return {"hold_id": self.hold_id, "idempotency_key": key}


class CancelSnapshot(_Snap):
    action: Literal["cancel"] = "cancel"
    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: AwareDatetime

    def dispatch_args(self, key: str) -> dict[str, Any]:
        return {"booking_id": self.booking_id, "idempotency_key": key}


class RescheduleSnapshot(_Snap):
    action: Literal["reschedule"] = "reschedule"
    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    old_slot: AwareDatetime
    slot: AwareDatetime  # the new slot

    def dispatch_args(self, key: str) -> dict[str, Any]:
        return {"booking_id": self.booking_id, "slot": self.slot.isoformat(), "idempotency_key": key}


Snapshot = Annotated[BookSnapshot | CancelSnapshot | RescheduleSnapshot, Field(discriminator="action")]
SNAPSHOT: TypeAdapter[BookSnapshot | CancelSnapshot | RescheduleSnapshot] = TypeAdapter(Snapshot)


def snapshot_hash(snapshot: BookSnapshot | CancelSnapshot | RescheduleSnapshot) -> str:
    canonical = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
