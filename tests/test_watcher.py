"""Stage 6b: proactive follow-up when a provider cancels (PDF §3; E24, E44).

Invariants:
- detection needs no user message: the provider's event is ingested and handled by workers;
- the source of truth decides: a false or stale event changes nothing and notifies nobody;
- a replacement is searched within the original request's constraints and confirmed saved
  preferences, never with the provider who cancelled;
- nothing is rebooked without fresh approval of a new card; cards for the cancelled booking are
  invalidated;
- duplicate deliveries and crashes produce one event, one follow-up and one notification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.llm.scripted import text
from errand_agent.memory.facts import EarliestTimeValue
from errand_agent.memory.store import Actor
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workers.jobs import JobWorker
from errand_agent.workers.outbox import Notification, Outbox
from errand_agent.workers.watcher import CancellationWatcher
from errand_agent.workflow.budget import RunManager
from errand_agent.workflow.parsing import LOCAL_TZ
from errand_agent.workflow.ranking import RankedOption
from tests.helpers import count
from tests.test_agent_scenarios import AC_MOVE, SEARCH_TO_CARD, Chat, comply, extract
from tests.workflow_helpers import AC_BOOKING

pytestmark = pytest.mark.invariant
FRIDAY = date(2026, 11, 6)


class CrashError(Exception):
    pass


@dataclass
class Env:
    db: Database
    clock: FakeClock
    backend: MockBackend
    tools: ToolClient
    chat: Chat
    watcher: CancellationWatcher
    worker: JobWorker
    outbox: Outbox

    def session_chat(self, session_id: int, receipt: object = None) -> Chat:
        other = Chat(
            self.db, self.clock, self.tools, ToolContext(user_id=DEMO_USER, session_id=session_id), self.chat.memory
        )
        other.receipt = receipt
        return other

    def followup_session(self) -> int:
        with self.db.txn() as conn:
            row = conn.execute("SELECT followup_session_id FROM provider_events").fetchone()
        assert row is not None
        return int(row["followup_session_id"])

    def options(self, session_id: int) -> list[RankedOption]:
        with self.db.txn() as conn:
            row = conn.execute("SELECT workspace FROM sessions WHERE id = %s", (session_id,)).fetchone()
        assert row is not None
        return [RankedOption.model_validate(o) for o in (row["workspace"] or {}).get("options", [])]

    def bookings(self) -> int:
        return count(
            self.db, "SELECT count(*) FROM mock_bookings WHERE user_id = %s AND hold_id IS NOT NULL", (DEMO_USER,)
        )


@pytest.fixture
def env(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, ctx: ToolContext) -> Env:
    chat = Chat(db, clock, tools, ctx)
    claims = ClaimManager(db, clock, lease_seconds=60)
    runs = RunManager(db, clock)
    watcher = CancellationWatcher(db, clock, tools, chat.agent.gate, claims=claims, runs=runs, memory=chat.memory)
    worker = JobWorker(db, clock, chat.agent.executor, holder="worker-A", claims=claims, runs=runs, events=watcher)
    return Env(db, clock, backend, tools, chat, watcher, worker, Outbox(db, clock, chat.agent.gate))


def book_plumber(env: Env, message: str, budget: str) -> str:
    chat = env.chat
    chat.say(
        message,
        extract(
            intent="book",
            service="plumber",
            date_expression="this week",
            time_expression="mornings only",
            budget_expression=budget,
        ),
        comply("ask_user"),
        text("Which area are you in?"),
    )
    chat.say("Indiranagar", extract(area="indiranagar"), *SEARCH_TO_CARD)
    env.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    done = chat.say("yes", comply("book"))
    assert done.outcome is not None
    assert done.outcome.booking_id is not None
    return done.outcome.booking_id


def only(notes: list[Notification]) -> Notification:
    assert len(notes) == 1, [n.text for n in notes]
    return notes[0]


# ------------------------------------------------------------------ E24: replacement within constraints
@pytest.mark.scenario(
    "S26",
    "A booked provider cancels (simulated event)",
    expected="Detected without a user message; replacement within the original constraints, never the "
    "same provider; booked only after fresh approval",
    covers="PDF §3 proactive follow-up; E24",
)
def test_e24_cancellation_is_detected_and_a_replacement_needs_fresh_approval(env: Env) -> None:
    booking = book_plumber(
        env, "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.", "under ₹1,500"
    )
    model_calls = len(env.chat.model.requests)
    env.backend.simulate_provider_cancellation(booking)

    assert env.watcher.ingest() == 1  # no user message anywhere below until approval
    [result] = env.worker.run_due()
    assert (result.kind, result.status) == ("provider_cancellation", "done")
    assert len(env.chat.model.requests) == model_calls  # deterministic: no model involved

    followup = env.followup_session()
    note = only(env.outbox.fetch(DEMO_USER))
    assert note.session_id == followup
    assert note.receipt_id is not None
    assert f"Lokesh Plumbing cancelled your plumber booking {booking}" in note.text
    assert "Replacement plumber options in Indiranagar on Fri 06 Nov; mornings only; up to ₹1,499" in note.text
    assert "Please confirm this booking" in note.text
    options = env.options(followup)
    assert options
    for o in options:
        local = o.slot.astimezone(LOCAL_TZ)
        assert o.provider_id != "PLU-IND-03"  # never the provider who cancelled
        assert local.date() == FRIDAY
        assert time(9, 0) <= local.time() < time(12, 0)  # the original "mornings only"
        assert o.price_inr <= 1499  # the original budget
    assert env.bookings() == 1  # only the cancelled original: nothing rebooked automatically

    follow = env.session_chat(followup, note.receipt_id)
    env.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    done = follow.say("yes", comply("book"))
    assert done.state == "completed"
    assert "Booked and verified" in follow.text(done)
    assert env.bookings() == 2
    assert (
        count(
            env.db, "SELECT count(*) FROM mock_bookings WHERE id = %s AND status = 'cancelled_by_provider'", (booking,)
        )
        == 1
    )


@pytest.mark.scenario(
    "S36",
    "A provider cancels and nothing else fits",
    expected="User notified with the reasons; nothing loosened; no card",
    covers="PDF §3 proactive follow-up; E24",
)
def test_e24_no_replacement_within_constraints_is_explained_not_loosened(env: Env) -> None:
    booking = book_plumber(env, "Find a plumber for my kitchen tap this week, mornings only, under ₹600.", "under ₹600")
    env.backend.simulate_provider_cancellation(booking)
    env.watcher.ingest()
    env.worker.run_due()
    note = only(env.outbox.fetch(DEMO_USER))
    assert note.receipt_id is None  # nothing to approve
    assert "found none" in note.text
    assert "over your budget" in note.text
    assert "Tell me another day or time" in note.text
    followup = env.followup_session()
    with env.db.txn() as conn:
        row = conn.execute("SELECT state, request FROM sessions WHERE id = %s", (followup,)).fetchone()
        proposals = conn.execute("SELECT count(*) AS n FROM proposals WHERE session_id = %s", (followup,)).fetchone()
    assert row is not None
    assert proposals is not None
    assert row["state"] == "collecting"
    assert row["request"]["service"] == "plumber"
    assert row["request"]["area"] == "indiranagar"  # the follow-up can continue from here
    assert proposals["n"] == 0
    assert env.bookings() == 1


def test_e24_saved_preferences_bound_a_replacement_for_a_booking_made_elsewhere(env: Env) -> None:
    env.chat.memory.remember(
        Actor(user_id=DEMO_USER),
        EarliestTimeValue(earliest=time(13, 0)),
        "global",
        statement="remember not before 1 pm",
    )
    env.backend.simulate_provider_cancellation(AC_BOOKING)  # history booking: no approved constraints
    env.watcher.ingest()
    env.worker.run_due()
    note = only(env.outbox.fetch(DEMO_USER))
    assert "Harish Cooling Care cancelled your AC servicing booking BK-H0002" in note.text
    for o in env.options(env.followup_session()):
        assert o.provider_id != "ACS-IND-01"
        assert o.slot.astimezone(LOCAL_TZ).time() >= time(13, 0)  # confirmed saved preference applied


def test_e24_a_pending_card_for_the_cancelled_booking_is_invalidated(env: Env) -> None:
    chat = env.chat
    card = chat.say(
        AC_MOVE,
        extract(intent="reschedule", service="ac_servicing", date_expression="Saturday"),
        comply("list_bookings"),
        comply("search_providers"),
        comply("select_option"),
    )
    assert card.state == "awaiting_approval"
    env.backend.simulate_provider_cancellation(AC_BOOKING)
    env.watcher.ingest()
    env.worker.run_due()
    late = chat.say("yes")
    assert "no longer current" in chat.text(late)
    assert late.state == "collecting"
    assert count(env.db, "SELECT count(*) FROM operations WHERE action = 'reschedule'") == 0


# ------------------------------------------------------------------ false events, duplicates, crashes (E44)
def test_an_event_for_a_booking_that_is_still_active_changes_nothing(env: Env) -> None:
    env.backend.emit_provider_event(AC_BOOKING)  # the feed says cancelled; the source of truth disagrees
    env.watcher.ingest()
    [result] = env.worker.run_due()
    assert result.detail.startswith("ignored")
    assert env.outbox.fetch(DEMO_USER) == []
    assert count(env.db, "SELECT count(*) FROM proposals") == 0
    assert (
        count(env.db, "SELECT count(*) FROM mock_bookings WHERE id = %s AND status = 'confirmed'", (AC_BOOKING,)) == 1
    )


@pytest.mark.scenario(
    "S37",
    "The same cancellation is delivered three times",
    expected="One event, one follow-up, one notification; redelivered with a stable ID until acknowledged",
    covers="PDF §3 proactive follow-up; E44",
)
def test_e44_duplicate_deliveries_make_one_event_one_follow_up_one_notification(env: Env) -> None:
    env.backend.simulate_provider_cancellation(AC_BOOKING, events=3)
    assert env.watcher.ingest() == 1
    assert env.watcher.ingest() == 0
    assert count(env.db, "SELECT count(*) FROM provider_events") == 1
    assert count(env.db, "SELECT count(*) FROM provider_event_sources") == 3
    assert count(env.db, "SELECT count(*) FROM jobs WHERE kind = 'provider_cancellation'") == 1
    env.worker.run_due()
    assert env.worker.run_due() == []
    first = only(env.outbox.fetch(DEMO_USER))
    assert [n.id for n in env.outbox.fetch(DEMO_USER)] == [first.id]  # same ID until acknowledged
    env.outbox.ack(DEMO_USER, [first.id])
    assert env.outbox.fetch(DEMO_USER) == []


def test_e44_a_crash_mid_follow_up_still_yields_one_card_and_one_notification(env: Env) -> None:
    crashed = {"done": False}

    def crash_once(point: str) -> None:
        if point == "after_hold" and not crashed["done"]:
            crashed["done"] = True
            raise CrashError("worker died after holding a replacement")

    env.watcher.fault = crash_once
    env.backend.simulate_provider_cancellation(AC_BOOKING)
    env.watcher.ingest()
    [failed] = env.worker.run_due()
    assert failed.status == "requeued"
    env.clock.advance(timedelta(seconds=5))
    [done] = env.worker.run_due()
    assert done.status == "done"
    followup = env.followup_session()
    assert count(env.db, "SELECT count(*) FROM sessions WHERE id >= %s", (followup,)) == 1  # one follow-up
    assert (
        count(env.db, "SELECT count(*) FROM proposals WHERE session_id = %s AND status = 'pending'", (followup,)) == 1
    )
    only(env.outbox.fetch(DEMO_USER))
    assert env.bookings() == 0  # holds at most; never a booking without approval


def test_e44_the_notification_waits_for_an_offline_user(env: Env) -> None:
    env.backend.simulate_provider_cancellation(AC_BOOKING)
    env.watcher.ingest()
    env.worker.run_due()
    env.clock.advance(timedelta(hours=2))  # the user comes back much later
    note = only(env.outbox.fetch(DEMO_USER))
    assert "Harish Cooling Care cancelled" in note.text


def test_the_runner_takes_a_cancellation_from_event_to_notification(env: Env) -> None:
    from errand_agent.workers.runner import Runner

    env.backend.simulate_provider_cancellation(AC_BOOKING, events=2)
    ticks = Runner(env.watcher, env.worker).drain()
    assert ticks[0].ingested == 1
    assert [j.kind for t in ticks for j in t.jobs] == ["provider_cancellation"]
    assert ticks[-1].ingested == 0
    assert not ticks[-1].jobs
    only(env.outbox.fetch(DEMO_USER))
