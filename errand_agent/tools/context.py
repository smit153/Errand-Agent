"""Internal execution context passed alongside (never inside) tool arguments."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Fence(BaseModel):
    """Proof that the caller holds the current claim and a reserved attempt for an operation.

    The backend re-verifies all of it inside its mutation transaction before any effect.
    """

    model_config = ConfigDict(frozen=True)

    holder: str
    generation: int
    operation_id: UUID
    attempt_id: UUID


class ToolContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    session_id: int
    run_id: UUID | None = None
    fence: Fence | None = None
