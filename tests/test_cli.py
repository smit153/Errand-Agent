"""Stage 8b: the thin terminal client against the real API (through an in-process transport)."""

from __future__ import annotations

import ast
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.cli import ApiError, ChatClient, run_chat, run_trace, urllib_transport
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.llm.scripted import text
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.tools.client import ToolClient
from errand_agent.workers.jobs import JobWorker
from errand_agent.workers.runner import Runner
from errand_agent.workers.watcher import CancellationWatcher
from errand_agent.workflow.approval import ApprovalGate
from errand_agent.workflow.budget import RunManager
from errand_agent.workflow.executor import OperationExecutor
from tests.test_agent_scenarios import PLUMBER, SEARCH_TO_CARD, comply, extract
from tests.test_api import Api, make_api
from tests.workflow_helpers import AC_BOOKING


def transport(api: Api, *, lose_first_reply: bool = False) -> Any:
    state = {"lost": False}

    def send(method: str, path: str, body: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
        del timeout
        r = api.client.request(method, path, json=body)
        if lose_first_reply and path.endswith("/messages") and not state["lost"]:
            state["lost"] = True
            raise ConnectionResetError("connection dropped after the server handled the message")
        return r.status_code, r.json()

    return send


@pytest.fixture
def api(db: Database, clock: FakeClock, backend: MockBackend, tools: ToolClient) -> Iterator[Api]:
    made = make_api(db, clock, backend, tools, demo=True)
    yield made
    made.client.close()


def client(api: Api, **kw: Any) -> tuple[ChatClient, io.StringIO]:
    out = io.StringIO()
    return ChatClient(transport(api, **kw), out=out, sleep=lambda _s: None), out


def script_card(api: Api) -> None:
    api.model.extend(
        [
            extract(
                intent="book",
                service="plumber",
                date_expression="this week",
                time_expression="mornings only",
                budget_expression="under ₹1,500",
            ),
            comply("ask_user"),
            text("Which area are you in?"),
            extract(area="indiranagar"),
            *SEARCH_TO_CARD,
        ]
    )


def test_a_full_booking_through_the_client(api: Api) -> None:
    c, out = client(api)
    script_card(api)
    api.model.extend([comply("book")])
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    run_chat(c, None, stdin=io.StringIO(f"{PLUMBER}\nIndiranagar\nyes\n/quit\n"))
    shown = out.getvalue()
    assert "Which area are you in?" in shown
    assert "Please confirm this booking" in shown
    assert "Booked and verified" in shown
    assert "[completed]" in shown
    assert api.bookings() == 1


def test_a_lost_response_is_retried_with_the_same_id_and_replayed(api: Api) -> None:
    c, out = client(api, lose_first_reply=True)
    c.start(None)
    api.model.extend(
        [
            extract(
                intent="book",
                service="plumber",
                date_expression="this week",
                time_expression="mornings only",
                budget_expression="under ₹1,500",
            ),
            comply("ask_user"),
            text("Which area are you in?"),
        ]
    )
    c.say(PLUMBER)
    assert "(replayed)" in out.getvalue()
    assert api.model.remaining == 0
    assert len(api.model.requests) == 3  # the turn ran once, not twice


def test_a_card_from_a_notification_is_approved_in_its_own_conversation(
    api: Api, db: Database, clock: FakeClock, tools: ToolClient
) -> None:
    c, out = client(api)
    c.start(None)
    first = c.session_id
    claims, runs = ClaimManager(db, clock, lease_seconds=60), RunManager(db, clock)
    watcher = CancellationWatcher(db, clock, tools, ApprovalGate(db, clock), claims=claims, runs=runs)
    worker = JobWorker(
        db, clock, OperationExecutor(db, clock, tools), holder="w", claims=claims, runs=runs, events=watcher
    )
    assert api.client.post("/demo/provider-cancel", json={"booking_id": AC_BOOKING}).status_code == 200
    Runner(watcher, worker).drain()
    assert c.check_notifications() == 1
    assert c.pending_card is not None
    assert "Harish Cooling Care cancelled" in out.getvalue()
    api.model.extend([comply("book")])
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    reply = c.say("yes")
    assert c.session_id != first
    assert "(switching to conversation" in out.getvalue()
    assert reply["state"] == "completed"
    assert api.bookings() == 1


def test_trace_once_prints_formatted_lines(api: Api) -> None:
    c, out = client(api)
    c.start(None)
    api.model.extend(
        [
            extract(
                intent="book",
                service="plumber",
                date_expression="this week",
                time_expression="mornings only",
                budget_expression="under ₹1,500",
            ),
            comply("ask_user"),
            text("Which area are you in?"),
        ]
    )
    c.say(PLUMBER)
    run_trace(c, session_id=c.session_id, follow=False, raw=False)
    shown = out.getvalue()
    assert "state.transition" in shown or "model.call" in shown
    assert "extract #1 accepted update_request" in shown


def test_errors_are_reported_not_raised_from_the_chat_loop(api: Api) -> None:
    c, out = client(api)
    with pytest.raises(ApiError):
        c.start(999_999)
    c.start(None)
    run_chat(c, c.session_id, stdin=io.StringIO("/session\n/quit\n"))
    assert f"(conversation #{c.session_id})" in out.getvalue()


def test_the_client_only_talks_to_a_local_server() -> None:
    with pytest.raises(SystemExit):
        urllib_transport("http://example.com:8765")
    urllib_transport("http://127.0.0.1:8765")


def test_the_client_uses_only_the_standard_library() -> None:
    tree = ast.parse((Path(__file__).parents[1] / "errand_agent" / "cli.py").read_text(encoding="utf-8"))
    modules = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    modules |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules - {"__future__"} <= set(sys.stdlib_module_names)


def test_resuming_shows_the_pending_card_again_and_yes_approves_it(api: Api) -> None:
    first, _ = client(api)
    first.start(None)
    script_card(api)
    first.say(PLUMBER)
    first.say("Indiranagar")
    resumed, out = client(api)  # a new client process: it never saw the original receipt
    api.model.extend([comply("book")])
    api.backend.configure(ScenarioConfig(book_outcomes=["success"]))
    resumed.start(first.session_id)
    assert "Please confirm this booking" in out.getvalue()
    assert resumed.receipt_id is not None
    assert resumed.receipt_id != first.receipt_id  # a fresh receipt for the card shown again
    assert resumed.say("yes")["state"] == "completed"
    assert api.bookings() == 1


def test_trace_new_only_skips_the_history(api: Api) -> None:
    """The demo's trace pane starts empty instead of printing the seeded history first."""
    c, out = client(api)
    c.start(None)
    api.model.extend(
        [
            extract(
                intent="book",
                service="plumber",
                date_expression="this week",
                time_expression="mornings only",
                budget_expression="under ₹1,500",
            ),
            comply("ask_user"),
            text("Which area are you in?"),
        ]
    )
    c.say(PLUMBER)
    run_trace(c, session_id=None, follow=False, raw=False, new_only=True)
    shown = out.getvalue()
    assert "(showing new trace lines only)" in shown
    assert "extract #1" not in shown
