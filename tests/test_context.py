"""Stage 5c: context management (PDF §3; DECISIONS §§3, 10, 18; E21, E22).

- 50+ seeded memories in every lifecycle state and a long prior conversation: only relevant,
  current, confirmed memories reach the model; nothing marked CANARY ever does.
- Every request is at most 4,096 bytes (messages + tool schemas), with room kept for a retry.
- Drop order: reviews first, then the lowest-ranked memories; required content that cannot
  fit is never sent and the user gets an explicit answer.
- The exact context of every model call is logged, and the log matches what the model received.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.llm.caller import ModelCaller
from errand_agent.llm.context import (
    CAP_BYTES,
    RETRY_RESERVE_BYTES,
    ContextOverflowError,
    OptionalSection,
    build,
    request_bytes,
)
from errand_agent.llm.scripted import call
from errand_agent.llm.types import Message
from errand_agent.memory.seed import CANARY, seed_memories, seed_prior_conversation
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from tests.helpers import count
from tests.memory_helpers import TopicEmbedder
from tests.test_agent_scenarios import PLUMBER, SEARCH_TO_CARD, Chat, extract

PLUMBER_EXTRACT = extract(
    intent="book",
    service="plumber",
    date_expression="this week",
    time_expression="mornings only",
    budget_expression="under ₹1,500",
)


@pytest.fixture
def seeded(db: Database, clock: FakeClock, tools: ToolClient, ctx: ToolContext) -> Chat:
    emb = TopicEmbedder()
    store = MemoryStore(db, clock, emb)
    seed_memories(db, clock, store, DEMO_USER)
    seed_prior_conversation(db, clock, DEMO_USER)
    return Chat(db, clock, tools, ctx, store)


def _request_text(chat: Chat) -> list[str]:
    return [
        json.dumps({"m": [m.model_dump() for m in r.messages], "t": r.tools}, ensure_ascii=False)
        for r in chat.model.requests
    ]


def _model_call_payloads(db: Database, session_id: int) -> list[dict[str, Any]]:
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT payload FROM trace_events WHERE kind = 'model.call' AND session_id = %s ORDER BY id", (session_id,)
        ).fetchall()
    return [r["payload"] for r in rows]


def _eligible_numbers(db: Database, model: str, scopes: tuple[str, ...]) -> set[int]:
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT display_no FROM memories WHERE user_id = %s AND status = 'active' AND confirmed"
            " AND embedding_model = %s AND scope = ANY(%s)",
            (DEMO_USER, model, list(scopes)),
        ).fetchall()
    return {int(r["display_no"]) for r in rows}


# ------------------------------------------------------------------ E21
def test_e21_seed_has_fifty_plus_memories_in_every_state_and_a_long_history(seeded: Chat, db: Database) -> None:
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM memories WHERE user_id = %s GROUP BY status", (DEMO_USER,)
        ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    assert sum(by_status.values()) >= 50
    assert set(by_status) == {"active", "unconfirmed", "forgotten", "superseded", "pending_embedding"}
    assert count(db, "SELECT count(*) FROM memories WHERE user_id <> %s", (DEMO_USER,)) >= 6
    assert count(db, "SELECT count(*) FROM trace_events WHERE payload::text LIKE '%%prior chat%%'") >= 100


@pytest.mark.invariant
@pytest.mark.scenario(
    "S22",
    "57 seeded memories and 240 prior messages",
    expected="Only relevant, current, confirmed memories reach the model; no "
    "forgotten/unconfirmed/stale/other-user facts; no prior chat",
    covers="PDF §3 context management; E21",
)
def test_e21_only_relevant_current_confirmed_memories_reach_the_model(seeded: Chat, db: Database) -> None:
    card = seeded.say(PLUMBER, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    assert card.state == "awaiting_approval"
    sent = _request_text(seeded)

    # Nothing forgotten, superseded, pending, unconfirmed, old-model, another user's, or from prior chats.
    assert all(CANARY not in s for s in sent)
    assert all("11:00 AM" not in s for s in sent)  # the unconfirmed inferred plumber time
    assert all("Koramangala" not in s for s in sent)  # prior sessions' requests
    # Irrelevant eligible facts don't make it either: ranking + threshold, not just filtering.
    assert all("friendly dog" not in s for s in sent)
    assert all("balcony" not in s for s in sent)

    payloads = _model_call_payloads(db, seeded.ctx.session_id)
    eligible = _eligible_numbers(db, seeded.memory.embedder.model, ("global", "plumber"))
    with db.txn() as conn:
        retrieved = [
            [f"#{i['display_no']}" for i in r["payload"]["items"]]
            for r in conn.execute(
                "SELECT payload FROM trace_events WHERE kind = 'memory.retrieved' AND session_id = %s ORDER BY id",
                (seeded.ctx.session_id,),
            ).fetchall()
        ]
    assert retrieved
    assert all(len(r) <= 6 for r in retrieved)
    assert all(int(k.lstrip("#")) in eligible for r in retrieved for k in r)  # filtered before ranking
    included_any = False
    for p in payloads:
        plan = p["context"]["plan"]
        if plan is None or "memories" not in plan["sections"]:
            continue
        included = plan["sections"]["memories"]["included"]
        included_any = included_any or bool(included)
        assert {int(k.lstrip("#")) for k in included} <= eligible
        assert any(included == r[: len(included)] for r in retrieved)  # best-ranked kept, lowest dropped first
    assert included_any
    assert any("Kitchen tap is a single-lever mixer" in s for s in sent)  # a relevant plumber note did arrive


@pytest.mark.invariant
@pytest.mark.scenario(
    "S23",
    "The context sent each turn is logged",
    expected="Every model call's logged messages/tools equal what the model received, with byte counts",
    covers="PDF §3 log context each turn and prove it; E21",
)
def test_e21_the_logged_context_is_exactly_what_the_model_received(seeded: Chat, db: Database) -> None:
    seeded.say(PLUMBER, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    payloads = _model_call_payloads(db, seeded.ctx.session_id)
    assert len(payloads) == len(seeded.model.requests)
    for logged, request in zip(payloads, seeded.model.requests, strict=True):
        assert logged["messages"] == [m.model_dump() for m in request.messages]
        assert logged["tools"] == request.tools
        assert logged["context"]["request_bytes"] == request_bytes(request.messages, request.tools)


# ------------------------------------------------------------------ E22
@pytest.mark.invariant
@pytest.mark.scenario(
    "S24",
    "Long reviews and many memories compete for space",
    expected="Every request within the 4,096-byte cap; reviews dropped before memories",
    covers="PDF §3 fixed context budget; E22",
)
def test_e22_every_request_fits_the_cap_with_room_for_a_retry(seeded: Chat, db: Database) -> None:
    with db.txn() as conn:  # long reviews put pressure on the review section
        for i in range(8):
            conn.execute(
                "INSERT INTO mock_reviews (id, provider_id, author, rating, body)"
                " VALUES (%s, 'PLU-IND-03', 'r', 4, %s)",
                (f"LONG{i}", f"Long review {i}: " + "the plumber arrived on time and fixed the tap neatly. " * 6),
            )
    seeded.say(PLUMBER, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    for r in seeded.model.requests:
        assert request_bytes(r.messages, r.tools) <= CAP_BYTES - RETRY_RESERVE_BYTES
    select = next(
        p
        for p in _model_call_payloads(db, seeded.ctx.session_id)
        if any(t["function"]["name"] == "select_option" for t in p["tools"])
    )
    sections = select["context"]["plan"]["sections"]
    assert 0 < len(sections["reviews"]["included"]) < sections["reviews"]["offered"]  # reviews dropped first
    assert sections["reviews"]["bytes"] <= 900
    assert sections["memories"]["included"]  # memories kept while reviews were dropped
    assert sections["memories"]["bytes"] <= 700


@pytest.mark.invariant
@pytest.mark.scenario(
    "S25",
    "User sends a message too long for the budget",
    expected="Refused with a clear message; no model call; nothing changed",
    covers="PDF §3 context budget, adversarial input; E22",
)
def test_e22_an_oversized_message_is_refused_before_any_model_call(seeded: Chat, db: Database) -> None:
    huge = "Find a plumber for my kitchen tap, " + "it is really very urgent and " * 120
    reply = seeded.say(huge)
    assert reply.messages == [ContextOverflowError.user_message]
    assert seeded.model.requests == []
    assert reply.state == "collecting"
    with db.txn() as conn:
        row = conn.execute(
            "SELECT payload FROM trace_events WHERE kind = 'agent.reply' AND session_id = %s ORDER BY id DESC LIMIT 1",
            (seeded.ctx.session_id,),
        ).fetchone()
    assert row is not None
    assert row["payload"]["exit"] == "error:ContextOverflowError"


def test_e22_repair_requests_stay_within_the_cap(seeded: Chat) -> None:
    bad = call("update_request", service="plumber", area="atlantis" * 400)
    seeded.say(PLUMBER, bad, bad, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    retries = [r for r in seeded.model.requests if len(r.messages) == 3]
    assert len(retries) == 2
    assert all(request_bytes(r.messages, r.tools) <= CAP_BYTES for r in retries)


def test_e22_at_most_four_embedding_requests_per_run(seeded: Chat) -> None:
    emb = seeded.memory.embedder
    assert isinstance(emb, TopicEmbedder)
    before = emb.calls
    seeded.say(PLUMBER, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    made = emb.calls - before
    assert 1 <= made <= 4
    # BUG-009: every embedding request is also charged to the run, so the trace's counters are true.
    assert count(seeded.db, "SELECT coalesce(sum(embedding_calls), 0) FROM runs") == made


def test_unavailable_embedder_means_no_memories_not_a_failure(seeded: Chat, db: Database) -> None:
    emb = seeded.memory.embedder
    assert isinstance(emb, TopicEmbedder)
    emb.available = False
    card = seeded.say(PLUMBER, PLUMBER_EXTRACT, *SEARCH_TO_CARD)
    assert card.state == "awaiting_approval"
    assert all("Saved preferences of this user" not in s for s in _request_text(seeded))
    assert (
        count(
            db,
            "SELECT count(*) FROM trace_events WHERE kind = 'memory.retrieved' AND payload->>'unavailable' IS NOT NULL",
        )
        >= 1
    )


# ------------------------------------------------------------------ builder unit tests
def _section(name: str, n: int, size: int, cap: int = 10_000) -> OptionalSection:
    return OptionalSection(
        name=name,
        items=[(f"{name}{i}", f"{name}-{i}:" + "x" * size) for i in range(n)],
        max_bytes=cap,
        render="\n".join,
    )


def test_required_content_that_cannot_fit_is_never_built() -> None:
    with pytest.raises(ContextOverflowError):
        build("policy", ["x" * 5000], [], [])


def test_lower_priority_sections_drop_first_and_low_ranked_items_before_high() -> None:
    prompt = build("p", ["task"], [_section("mem", 5, 100), _section("rev", 5, 100)], [], cap=1000, reserve=200)
    mem = prompt.report.sections["mem"]
    rev = prompt.report.sections["rev"]
    assert mem.included == ["mem0", "mem1", "mem2", "mem3", "mem4"]
    assert rev.included == ["rev0"]  # reviews (later section) lose space first
    assert prompt.report.request_bytes <= 800
    tight = build("p", ["task"], [_section("mem", 5, 100), _section("rev", 5, 100)], [], cap=600, reserve=200)
    assert tight.report.sections["mem"].included == ["mem0", "mem1"]  # highest-ranked kept
    assert tight.report.sections["rev"].included == []


def test_section_caps_and_excerpts() -> None:
    sec = OptionalSection(
        name="rev",
        items=[("a", "y" * 1000), ("b", "z" * 1000)],
        max_bytes=700,
        render="\n".join,
        item_max_chars=300,
    )
    prompt = build("p", ["t"], [sec], [])
    assert prompt.report.sections["rev"].included == ["a", "b"]
    assert "y" * 299 + "…" in prompt.messages[1].content


def test_diagnostics_are_bounded_in_bytes() -> None:
    diag = ModelCaller._diagnostic("€" * 5000)  # 3 bytes per character
    message = Message(role="user", content=diag)
    assert request_bytes([message], []) - request_bytes([], []) <= RETRY_RESERVE_BYTES


def test_request_bytes_counts_tools_too() -> None:
    m = [Message(role="user", content="hi")]
    assert request_bytes(m, [{"x": "y" * 100}]) > request_bytes(m, []) + 100
