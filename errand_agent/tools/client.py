"""ToolClient: the single boundary between the workflow and the fixed tool contracts.

- Validates raw arguments into the contract's Pydantic input model (invalid → no-effect rejection).
- Passes internal context (user, session, run, fence) beside the arguments, never inside them.
- Commits a write-ahead `tool.attempt_started` event just before each tool call, then one sanitized
  `tool.call` event with latency and outcome (same `call_id`). A crashed call keeps its start event.
- Exposes fault-injection points so tests can simulate a crash before/after the backend call.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from errand_agent import trace
from errand_agent.clock import Clock
from errand_agent.db import Database
from errand_agent.mocks.backend import MockBackend
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolError, ToolRejectedError
from errand_agent.tools.models import (
    BookInput,
    BookOutput,
    CancelInput,
    CancelOutput,
    GetBookingStatusInput,
    GetBookingStatusOutput,
    GetProviderInput,
    GetProviderOutput,
    HoldSlotInput,
    HoldSlotOutput,
    ListBookingsInput,
    ListBookingsOutput,
    RescheduleInput,
    RescheduleOutput,
    SearchProvidersInput,
    SearchProvidersOutput,
)

log = logging.getLogger(__name__)

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)
FaultHook = Callable[[str, str, ToolContext], None]  # (point, tool, ctx); may raise to simulate a crash


class ToolClient:
    def __init__(self, backend: MockBackend, db: Database, clock: Clock, *, fault: FaultHook | None = None) -> None:
        self._backend = backend
        self._db = db
        self._clock = clock
        self.fault = fault

    # ------------------------------------------------------------------ contracts
    def search_providers(self, ctx: ToolContext, args: dict[str, Any]) -> SearchProvidersOutput:
        return self._invoke("search_providers", SearchProvidersInput, args, ctx, self._backend.search_providers)

    def get_provider(self, ctx: ToolContext, args: dict[str, Any]) -> GetProviderOutput:
        return self._invoke("get_provider", GetProviderInput, args, ctx, self._backend.get_provider)

    def hold_slot(self, ctx: ToolContext, args: dict[str, Any]) -> HoldSlotOutput:
        return self._invoke("hold_slot", HoldSlotInput, args, ctx, self._backend.hold_slot)

    def book(self, ctx: ToolContext, args: dict[str, Any]) -> BookOutput:
        return self._invoke("book", BookInput, args, ctx, self._backend.book)

    def get_booking_status(self, ctx: ToolContext, args: dict[str, Any]) -> GetBookingStatusOutput:
        return self._invoke("get_booking_status", GetBookingStatusInput, args, ctx, self._backend.get_booking_status)

    def cancel(self, ctx: ToolContext, args: dict[str, Any]) -> CancelOutput:
        return self._invoke("cancel", CancelInput, args, ctx, self._backend.cancel)

    def reschedule(self, ctx: ToolContext, args: dict[str, Any]) -> RescheduleOutput:
        return self._invoke("reschedule", RescheduleInput, args, ctx, self._backend.reschedule)

    def list_bookings(self, ctx: ToolContext, args: dict[str, Any]) -> ListBookingsOutput:
        return self._invoke("list_bookings", ListBookingsInput, args, ctx, self._backend.list_bookings)

    # ------------------------------------------------------------------ plumbing
    def _invoke(
        self,
        tool: str,
        model: type[In],
        args: dict[str, Any],
        ctx: ToolContext,
        fn: Callable[[In, ToolContext], Out],
    ) -> Out:
        call_id = uuid4()
        started = time.perf_counter()
        try:
            inp = model.model_validate(args)
        except ValidationError as exc:
            # Rejected before reaching the tool: there is no attempt, only the rejection event.
            err = ToolRejectedError(tool, "invalid_arguments", _validation_summary(exc))
            self._finished(tool, call_id, args, ctx, started, error=err)
            raise err from exc
        dumped = inp.model_dump(mode="json")
        # Write-ahead: committed before the call, so a call whose worker crashes still shows.
        self._record("tool.attempt_started", self._base(tool, call_id, dumped, ctx), ctx)
        try:
            if self.fault:
                self.fault("before_backend", tool, ctx)
            result = fn(inp, ctx)
            if self.fault:
                self.fault("after_backend", tool, ctx)
        except ToolError as exc:
            self._finished(tool, call_id, dumped, ctx, started, error=exc)
            raise
        self._finished(tool, call_id, dumped, ctx, started, result=result)
        return result

    @staticmethod
    def _base(tool: str, call_id: UUID, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "tool": tool,
            "call_id": str(call_id),
            "args": args,
            "attempt_id": str(ctx.fence.attempt_id) if ctx.fence else None,
            "claim_generation": ctx.fence.generation if ctx.fence else None,
        }

    def _finished(
        self,
        tool: str,
        call_id: UUID,
        args: dict[str, Any],
        ctx: ToolContext,
        started: float,
        *,
        result: BaseModel | None = None,
        error: ToolError | None = None,
    ) -> None:
        payload = self._base(tool, call_id, args, ctx)
        payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if error is not None:
            payload["error"] = {"type": type(error).__name__, "code": error.code, "effect": error.effect}
        if result is not None:
            payload["result"] = result.model_dump(mode="json")
        self._record("tool.call", payload, ctx)

    def _record(self, kind: str, payload: dict[str, Any], ctx: ToolContext) -> None:
        try:
            with self._db.txn() as conn:
                trace.record(
                    conn,
                    kind,
                    payload,
                    at=self._clock.now(),
                    run_id=ctx.run_id,
                    session_id=ctx.session_id,
                    operation_id=ctx.fence.operation_id if ctx.fence else ctx.operation_id,
                )
        except Exception:
            # Tracing must never change a tool's outcome; record the failure loudly instead.
            log.exception("failed to persist %s trace for %s", kind, payload.get("tool"))


def _validation_summary(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()[:5])
