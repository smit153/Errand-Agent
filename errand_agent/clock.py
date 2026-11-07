"""Injected clocks. Workflow and mocks never read process wall-clock directly."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic, thread-safe clock for tests and scripted scenarios."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware start time")
        self._now = start.astimezone(UTC)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("FakeClock cannot move backwards")
        with self._lock:
            self._now += delta
            return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FakeClock requires timezone-aware times")
        with self._lock:
            if when.astimezone(UTC) < self._now:
                raise ValueError("FakeClock cannot move backwards")
            self._now = when.astimezone(UTC)
