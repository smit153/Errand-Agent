"""Stage 8a: the local HTTP API over the tested engine (E05, E06 and input races through HTTP).

Invariants:
- only the local user's conversations are reachable; anything else is 404;
- a retried request (same client message ID) replays the stored reply and never runs a second
  turn; the same ID with different text is refused;
- concurrent messages to one conversation never produce two bookings;
- both confirmation forms work through HTTP, a forged or missing receipt never executes;
- notifications and the live trace are served; demo controls exist only when enabled.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from errand_agent.api.app import create_app
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.llm.scripted import ScriptedModel, text
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.workers.outbox import Outbox
from errand_agent.workflow.agent import Agent
from tests.helpers import count
from tests.memory_helpers import HashEmbedder
from tests.test_agent_scenarios import PLUMBER, SEARCH_TO_CARD, comply, extract

pytestmark = pytest.mark.invariant


@dataclass
class Api:
    client: TestClient
    model: ScriptedModel
    db: Database
    backend: MockBackend

    def session(self) -> int:
        r = self.client.post("/sessions")
        assert r.status_code == 201
        return int(r.json()["session_id"])

    def say(
        self, sid: int, message: str, *steps: Any, receipt: str | None = None, mid: str | None = None
    ) -> dict[str, Any]:
        self.model.extend(list(steps))
        body: dict[str, Any] = {"text": message, "client_message_id": mid or uuid4().hex}
        if receipt:
            body["receipt_id"] = receipt
        r = self.client.post(f"/sessions/{sid}/messages", json=body)
        assert r.status_code == 200, r.text
        assert self.model.remaining == 0
        out: dict[str, Any] = r.json()
        return out

    def card(self, sid: int) -> dict[str, Any]:
        self.say(
            sid,
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
        out = self.say(sid, "Indiranagar", extract(area="indiranagar"), *SEARCH_TO_CARD)
        assert out["state"] == "awaiting_approval"
        return out

    def bookings(self) -> int:
        return count(
            self.db, "SELECT count(*) FROM mock_bookings WHERE user_id = %s AND hold_id IS NOT NULL", (DEMO_USER,)
        )


def make_api(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient, *, demo: bool) -> Api:
    model = ScriptedModel()
    agent = Agent(db, clock, tools, model, memory=MemoryStore(db, clock, HashEmbedder()))
    app = create_app(
        db=db,
        clock=clock,
        agent=agent,
        outbox=Outbox(db, clock, agent.gate),
        backend=backend,
        user_id=DEMO_USER,
        demo_controls=demo,
    )
    return Api(TestClient(app), model, db, backend)


@pytest.fixture
def api(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient) -> Iterator[Api]:
    made = make_api(db, clock, backend, tools, demo=False)
    yield made
    made.client.close()


# ------------------------------------------------------------------ E05 through HTTP
def test_e05_plain_yes_with_the_displayed_receipt_books_once(api: Api) -> None:
    sid = api.session()
    card = api.card(sid)
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    done = api.say(sid, "yes", comply("book"), receipt=card["receipt_id"])
    assert done["state"] == "completed"
    assert "Booked and verified" in done["messages"][-1]
    assert api.bookings() == 1


def test_e05_token_confirmation_works_and_a_wrong_token_does_nothing(api: Api) -> None:
    sid = api.session()
    card = api.card(sid)
    token = card["messages"][-1].split("'confirm ")[1].split("'")[0]
    wrong = api.say(sid, "confirm ZZZZZZ", receipt=card["receipt_id"])
    assert "doesn't match" in wrong["messages"][-1]
    assert api.bookings() == 0
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    done = api.say(sid, f"confirm {token}", comply("book"), receipt=card["receipt_id"])
    assert done["state"] == "completed"
    assert api.bookings() == 1


def test_e05_yes_without_or_with_a_forged_receipt_never_executes(api: Api) -> None:
    sid = api.session()
    api.card(sid)
    assert api.say(sid, "yes")["state"] == "awaiting_approval"
    assert api.say(sid, "yes", receipt=str(uuid4()))["state"] == "awaiting_approval"
    assert api.bookings() == 0
    assert count(api.db, "SELECT count(*) FROM operations") == 0


# ------------------------------------------------------------------ E06: retries and repeats through HTTP
def test_e06_a_retried_request_replays_the_reply_without_a_second_turn(api: Api) -> None:
    sid = api.session()
    card = api.card(sid)
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    mid = uuid4().hex
    first = api.say(sid, "yes", comply("book"), receipt=card["receipt_id"], mid=mid)
    calls_before = len(api.model.requests)
    again = api.say(sid, "yes", receipt=card["receipt_id"], mid=mid)  # the client lost the response
    assert again["replayed"] is True
    assert again["messages"] == first["messages"]
    assert len(api.model.requests) == calls_before  # no new turn ran
    assert api.bookings() == 1
    clash = api.client.post(f"/sessions/{sid}/messages", json={"text": "no", "client_message_id": mid})
    assert clash.status_code == 409


def test_e06_a_second_yes_with_a_new_id_reports_the_same_booking(api: Api) -> None:
    sid = api.session()
    card = api.card(sid)
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    api.say(sid, "yes", comply("book"), receipt=card["receipt_id"])
    second = api.say(sid, "yes", receipt=card["receipt_id"])
    assert "Booked and verified" in second["messages"][-1]
    assert api.bookings() == 1
    assert count(api.db, "SELECT count(*) FROM operations") == 1


def test_concurrent_confirmations_never_book_twice(api: Api) -> None:
    sid = api.session()
    card = api.card(sid)
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    api.model.extend([comply("book"), comply("book")])  # more than enough for both racers
    barrier = threading.Barrier(2)
    results: list[int] = []

    def confirm() -> None:
        barrier.wait()
        r = api.client.post(
            f"/sessions/{sid}/messages",
            json={"text": "yes", "client_message_id": uuid4().hex, "receipt_id": card["receipt_id"]},
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200]
    assert api.bookings() == 1
    assert count(api.db, "SELECT count(*) FROM operations") == 1


# ------------------------------------------------------------------ ownership, notifications, trace, demo
def test_other_users_conversations_are_not_reachable(api: Api, db: Database, clock: FakeClock) -> None:
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
        row = conn.execute(
            "INSERT INTO sessions (user_id, created_at, updated_at) VALUES ('user-other', %s, %s) RETURNING id",
            (clock.now(), clock.now()),
        ).fetchone()
    assert row is not None
    other = int(row["id"])
    r = api.client.post(f"/sessions/{other}/messages", json={"text": "hi", "client_message_id": uuid4().hex})
    assert r.status_code == 404
    assert api.client.get(f"/sessions/{other}/trace").status_code == 404
    assert other not in [s["session_id"] for s in api.client.get("/sessions").json()]


def test_input_is_validated(api: Api) -> None:
    sid = api.session()
    for body in (
        {"text": "", "client_message_id": uuid4().hex},
        {"text": "hi", "client_message_id": "x"},
        {"text": "hi", "client_message_id": uuid4().hex, "extra": 1},
    ):
        assert api.client.post(f"/sessions/{sid}/messages", json=body).status_code == 422


def test_trace_feed_and_notifications(api: Api, db: Database, clock: FakeClock) -> None:
    sid = api.session()
    api.card(sid)
    lines = api.client.get("/trace").json()
    assert any(entry["kind"] == "model.call" and "accepted" in entry["line"] for entry in lines)
    assert any(entry["kind"] == "tool.call" and "search_providers" in entry["line"] for entry in lines)
    last = lines[-1]["id"]
    assert api.client.get(f"/trace?after={last}").json() == []
    from errand_agent.workers.outbox import notify

    with db.txn() as conn:
        notify(
            conn,
            user_id=DEMO_USER,
            session_id=sid,
            event_key="t1",
            kind="operation_outcome",
            text="hello",
            now=clock.now(),
        )
    [n] = api.client.get("/notifications").json()
    assert n["text"] == "hello"
    assert api.client.post("/notifications/ack", json={"ids": [n["id"]]}).json() == {"acked": 1}
    assert api.client.get("/notifications").json() == []


def test_demo_controls_exist_only_when_enabled(
    api: Api, db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient
) -> None:
    assert api.client.post("/demo/next-booking", json={"outcome": "timeout_booked"}).status_code == 404
    demo = make_api(db, clock, backend, tools, demo=True)
    try:
        assert demo.client.post("/demo/next-booking", json={"outcome": "timeout_booked"}).status_code == 200
        bookings = demo.client.get("/demo/bookings").json()
        assert any(b["id"] == "BK-H0002" for b in bookings)
        r = demo.client.post("/demo/provider-cancel", json={"booking_id": "BK-H0002"})
        assert r.status_code == 200
        assert demo.client.post("/demo/provider-cancel", json={"booking_id": "BK-H0002"}).status_code == 409
    finally:
        demo.client.close()
