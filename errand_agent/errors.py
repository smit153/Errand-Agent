"""Typed errors. Every error carries a user-safe message; internals stay in traces."""

from __future__ import annotations


class ErrandError(Exception):
    """Base class. `user_message` is safe to show; `str(self)` is for logs."""

    user_message: str = "Something went wrong, and I stopped before taking any further action."

    def __init__(self, detail: str, *, user_message: str | None = None) -> None:
        super().__init__(detail)
        if user_message is not None:
            self.user_message = user_message


class SessionBusyError(ErrandError):
    user_message = "This conversation is busy finishing another step. Please try again in a moment."


class StaleClaimError(ErrandError):
    """A worker's claim generation no longer matches; its results must be discarded."""

    user_message = "Another worker took over this conversation; I discarded an out-of-date result."


class ConcurrentUpdateError(ErrandError):
    """Optimistic revision check (CAS) failed."""

    user_message = "The conversation changed while I was working, so I didn't apply an out-of-date update."


class BudgetExhaustedError(ErrandError):
    def __init__(self, limit: str, detail: str) -> None:
        super().__init__(detail)
        self.limit = limit


class MigrationError(ErrandError):
    pass


class InvalidTransitionError(ErrandError):
    pass
