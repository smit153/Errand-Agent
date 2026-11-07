"""Stage 7: conversation-level scenarios for the PDF §3 evaluation list that were previously only
covered below the agent (executor/gate/recovery tests): timeout after possible success, timeout
with no booking, slot taken, reschedule slot conflict, repeated confirmation, a card changed after
it was shown, restart mid-booking, limit exhaustion and a tool failure handled in the background.

Each drives the full agent with a simulated user and a scripted model, and records what it
observed for the generated eval table.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.llm.scripted import text
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workers.jobs import JobWorker
from errand_agent.workers.outbox import Outbox
from errand_agent.workflow.agent import Agent
from errand_agent.workflow.budget import RunLimits, RunManager
from tests.helpers import count
from tests.test_agent_scenarios import AC_MOVE, PLUMBER, SEARCH_TO_CARD, Chat, comply, extract
from tests.workflow_helpers import AC_BOOKING

Observe = Callable[[str], None]


@pytest.fixture
def chat(db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> Chat:
    return Chat(db, clock, tools, ctx)


def card(chat: Chat) -> None:
    chat.say(
        PLUMBER,
        extract(
            intent="book",
            service="plumber",
            date_expression="this week",
            time_expression="mornings only",
            budget_expression="under ₹1,500",
        ),
        comply("ask_user"),
        text("Which area are you in?"),
    )
    reply = chat.say("Indiranagar", extract(area="indiranagar"), *SEARCH_TO_CARD)
    assert reply.state == "awaiting_approval"


def calls(db: Database, tool: str) -> list[dict[str, object]]:
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT payload FROM trace_events WHERE kind = 'tool.call' AND payload->>'tool' = %s ORDER BY id", (tool,)
        ).fetchall()
    return [r["payload"] for r in rows]


def worker(db: Database, clock: FakeClock, agent: Agent) -> JobWorker:
    return JobWorker(
        db,
        clock,
        agent.executor,
        holder="worker-A",
        claims=ClaimManager(db, clock, lease_seconds=60),
        runs=RunManager(db, clock),
    )


# ------------------------------------------------------------------ booking outcomes
@pytest.mark.invariant
@pytest.mark.scenario(
    "S27",
    "Booking times out after it actually succeeded",
    expected="Status is checked before anything else; exactly one booking; success shown only once verified",
    covers="PDF §3 timeout after possible success; E10",
)
def test_s27_timeout_after_success(chat: Chat, backend: MockBackend, observe: Observe) -> None:
    card(chat)
    backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"]))
    done = chat.say("yes", comply("book"))
    books, statuses = calls(chat.db, "book"), calls(chat.db, "get_booking_status")
    observe(f"book calls={len(books)}, status checks={len(statuses)}, bookings={chat.bookings()}")
    observe(f"reply: {done.messages[-1][:70]}…")
    assert done.state == "completed"
    assert done.outcome is not None
    assert done.outcome.verified
    assert len(books) == 1
    assert len(statuses) == 1
    assert chat.bookings() == 1
    assert "Booked and verified" in chat.text(done)


@pytest.mark.invariant
@pytest.mark.scenario(
    "S28",
    "Booking times out and did not happen",
    expected="Status check says not booked; one same-key retry; one booking; verified success",
    covers="PDF §3 timeout/retry correctness; E11",
)
def test_s28_timeout_without_booking_retries_with_the_same_key(
    chat: Chat, backend: MockBackend, observe: Observe
) -> None:
    card(chat)
    backend.configure(ScenarioConfig(book_outcomes=["timeout_lost", "success"]))
    done = chat.say("yes", comply("book"))
    keys = {str(c["args"]["idempotency_key"]) for c in calls(chat.db, "book")}  # type: ignore[index]
    observe(f"book calls={len(calls(chat.db, 'book'))} with {len(keys)} key(s); bookings={chat.bookings()}")
    assert done.state == "completed"
    assert len(calls(chat.db, "book")) == 2
    assert len(keys) == 1
    assert chat.bookings() == 1


@pytest.mark.invariant
@pytest.mark.scenario(
    "S29",
    "Slot is taken at booking time",
    expected="Reported as not booked with the reason; no booking; old approval can't be reused",
    covers="PDF §3 tool failures / unexpected errors; E12",
)
def test_s29_slot_taken(chat: Chat, backend: MockBackend, observe: Observe) -> None:
    card(chat)
    backend.configure(ScenarioConfig(book_outcomes=["slot_taken"]))
    done = chat.say("yes", comply("book"))
    again = chat.say("yes")
    observe(f"state={done.state}, bookings={chat.bookings()}; repeat yes → {again.messages[-1][:60]}…")
    assert done.state == "conflict"  # the slot is gone: the user picks again, with a fresh approval
    assert "someone else took that slot" in chat.text(done)
    assert chat.bookings() == 0
    assert len(calls(chat.db, "book")) == 1  # the repeat "yes" sent nothing new


@pytest.mark.invariant
@pytest.mark.scenario(
    "S30",
    "Reschedule hits a slot conflict",
    expected="Conflict reported; original booking unchanged; nothing moved without a new approval",
    covers="PDF §3 slot conflict; E12",
)
def test_s30_reschedule_conflict(chat: Chat, backend: MockBackend, observe: Observe) -> None:
    reply = chat.say(
        AC_MOVE,
        extract(intent="reschedule", service="ac_servicing", date_expression="Saturday"),
        comply("list_bookings"),
        comply("search_providers"),
        comply("select_option"),
    )
    assert reply.state == "awaiting_approval"
    with chat.db.txn() as conn:
        row = conn.execute("SELECT snapshot FROM proposals WHERE status = 'pending'").fetchone()
        before = conn.execute("SELECT slot FROM mock_bookings WHERE id = %s", (AC_BOOKING,)).fetchone()
    assert row is not None
    assert before is not None
    backend.configure(ScenarioConfig(reschedule_conflict_slots=[row["snapshot"]["slot"]]))
    done = chat.say("yes", comply("reschedule"))
    with chat.db.txn() as conn:
        after = conn.execute("SELECT slot, status FROM mock_bookings WHERE id = %s", (AC_BOOKING,)).fetchone()
    assert after is not None
    observe(f"state={done.state}; booking slot unchanged={after['slot'] == before['slot']}, status={after['status']}")
    assert done.state in ("conflict", "failed")
    assert "isn't available" in chat.text(done)
    assert after["slot"] == before["slot"]
    assert after["status"] == "confirmed"


@pytest.mark.invariant
@pytest.mark.scenario(
    "S31",
    "User confirms the same booking twice",
    expected="One operation, one booking; the second 'yes' reports the same result",
    covers="PDF §3 repeated confirmation; E06",
)
def test_s31_repeated_confirmation(chat: Chat, backend: MockBackend, observe: Observe) -> None:
    card(chat)
    backend.configure(ScenarioConfig(book_outcomes=["success"]))
    first = chat.say("yes", comply("book"))
    second = chat.say("yes")
    ops = count(chat.db, "SELECT count(*) FROM operations")
    observe(f"operations={ops}, bookings={chat.bookings()}, book calls={len(calls(chat.db, 'book'))}")
    assert first.state == "completed"
    assert ops == 1
    assert chat.bookings() == 1
    assert len(calls(chat.db, "book")) == 1
    assert "Booked and verified" in chat.text(second)


@pytest.mark.invariant
@pytest.mark.scenario(
    "S32",
    "Approving a card that was replaced by a different slot",
    expected="The old card is refused with an explanation; only the newly shown card can be approved",
    covers="PDF §3 changed provider/slot/price invalidates approval; E07",
)
def test_s32_stale_card_after_a_change(chat: Chat, backend: MockBackend, observe: Observe) -> None:
    card(chat)
    old_receipt = chat.receipt
    replaced = chat.say("Option 2, please.", extract(option_number="2"), *SEARCH_TO_CARD)
    assert replaced.state == "awaiting_approval"
    assert chat.receipt != old_receipt
    stale = chat.agent.handle(chat.ctx.session_id, DEMO_USER, "yes", old_receipt)
    observe(f"stale yes → bookings={chat.bookings()}: {stale.messages[-1][:70]}…")
    assert chat.bookings() == 0
    assert "haven't taken any action" in "\n".join(stale.messages) or "no longer" in "\n".join(stale.messages)
    backend.configure(ScenarioConfig(book_outcomes=["success"]))
    done = chat.say("yes", comply("book"))
    observe(f"current card → {done.state}, bookings={chat.bookings()}")
    assert done.state == "completed"
    assert chat.bookings() == 1


# ------------------------------------------------------------------ restart, limits, background failure
class CrashError(Exception):
    pass


@pytest.mark.invariant
@pytest.mark.scenario(
    "S33",
    "Process dies right after the booking was sent",
    expected="After restart the outcome is verified, not guessed; exactly one booking; no second dispatch",
    covers="PDF §3 restart mid-booking; E13",
)
def test_s33_restart_mid_booking(
    chat: Chat, backend: MockBackend, db: Database, clock: FakeClock, tools: ToolClient, observe: Observe
) -> None:
    card(chat)
    backend.configure(ScenarioConfig(book_outcomes=["success"]))

    def crash(point: str, _op: UUID) -> None:
        if point == "after_dispatch":
            raise CrashError("process died before recording the result")

    chat.agent.executor.fault = crash
    crashed = chat.say("yes", comply("book"))
    assert crashed.state != "completed"
    restarted = Chat(db, clock, tools, chat.ctx, chat.memory)  # a new process: fresh agent, same database
    status = restarted.say("Did my booking go through?")
    observe(
        f"after crash state={crashed.state}; after restart → {status.state}, bookings={chat.bookings()}, "
        f"book calls={len(calls(db, 'book'))}"
    )
    assert status.state == "completed"
    assert "Booked and verified" in restarted.text(status)
    assert chat.bookings() == 1
    assert len(calls(db, "book")) == 1
    clock.advance(timedelta(minutes=10))
    worker(db, clock, restarted.agent).run_due()  # the dead turn's job: already resolved, nothing sent
    assert len(calls(db, "book")) == 1


@pytest.mark.invariant
@pytest.mark.scenario(
    "S34",
    "A turn runs out of its model-call allowance",
    expected="Clear stop message naming the limit; no tool call or booking after it; run exit recorded",
    covers="PDF §3 bounded execution / limit exhaustion; E23",
)
def test_s34_limit_exhaustion(chat: Chat, db: Database, clock: FakeClock, tools: ToolClient, observe: Observe) -> None:
    chat.agent = Agent(db, clock, tools, chat.model, run_limits=RunLimits(logical_model_calls=1), memory=chat.memory)
    reply = chat.say(
        PLUMBER,
        extract(
            intent="book",
            service="plumber",
            date_expression="this week",
            time_expression="mornings only",
            budget_expression="under ₹1,500",
        ),
    )
    tool_calls = count(db, "SELECT count(*) FROM trace_events WHERE kind = 'tool.call'")
    observe(f"reply: {reply.messages[-1]}; state={reply.state}; tool calls={tool_calls}")
    assert reply.messages[-1].startswith("I stopped because a safety limit was reached (logical_model_calls)")
    assert tool_calls == 0
    assert chat.bookings() == 0
    with db.txn() as conn:
        exit_row = conn.execute("SELECT exit_reason FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert exit_row is not None
    assert exit_row["exit_reason"] == "limit:logical_model_calls"


@pytest.mark.invariant
@pytest.mark.scenario(
    "S35",
    "Cancellation hits the provider's transient 503",
    expected="User told it will be retried; worker retries with the same key when due; verified; user notified once",
    covers="PDF §2 cancel 503 / §3 tool failures and exhausted retries; E14",
)
def test_s35_cancel_503_is_finished_in_the_background(
    chat: Chat, db: Database, clock: FakeClock, observe: Observe
) -> None:
    proposed = chat.say(
        "Cancel my AC servicing.", extract(intent="cancel", service="ac_servicing"), comply("list_bookings")
    )
    assert proposed.state == "awaiting_approval"
    first = chat.say("yes", comply("cancel"))
    assert first.state == "retry_wait"
    assert "retry automatically" in chat.text(first)
    clock.advance(timedelta(seconds=1))
    [job] = worker(db, clock, chat.agent).run_due()
    keys = {str(c["args"]["idempotency_key"]) for c in calls(db, "cancel")}  # type: ignore[index]
    notes = Outbox(db, clock, chat.agent.gate).fetch(DEMO_USER)
    observe(
        f"turn → retry_wait; worker job {job.status}; cancel calls={len(calls(db, 'cancel'))} "
        f"with {len(keys)} key; notifications={len(notes)}"
    )
    assert job.status == "done"
    assert len(calls(db, "cancel")) == 2
    assert len(keys) == 1
    assert count(db, "SELECT count(*) FROM mock_bookings WHERE id = %s AND status = 'cancelled'", (AC_BOOKING,)) == 1
    assert len(notes) == 1
    assert "Cancelled and verified" in notes[0].text
