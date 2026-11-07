"""Stage 5d: retrieval on recorded real nomic-embed-text vectors (offline part of E43).

`tests/fixtures/nomic_vectors.json` holds real vectors recorded from the pinned local model
(`scripts/record_embedding_fixtures.py`). These tests run the production filter/rank/threshold
code on real-model geometry without needing Ollama. A missing fixture fails; nothing is invented.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.memory.embedder import FixtureEmbedder
from errand_agent.memory.facts import NoteValue, ProviderValue, canonical_text
from errand_agent.memory.store import MIN_SCORE, Actor, MemoryStore
from tests.conftest import make_session

FIXTURES = Path(__file__).parent / "fixtures"
PAIRS: dict[str, Any] = json.loads((FIXTURES / "embedding_pairs.json").read_text(encoding="utf-8"))
RECORDED: dict[str, Any] = json.loads((FIXTURES / "nomic_vectors.json").read_text(encoding="utf-8"))
SERVICES = ["plumber", "electrician", "ac", "carpenter", "cleaning"]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _scores(topic: str) -> tuple[list[float], list[float]]:
    emb = FixtureEmbedder(RECORDED["vectors"], model=RECORDED["model"])
    q = emb.embed([PAIRS["queries"][topic]], kind="query")[0]
    relevant = emb.embed(PAIRS["documents"][topic], kind="document")
    others = [d for t in SERVICES if t != topic for d in PAIRS["documents"][t]]
    irrelevant = emb.embed(others, kind="document")
    return [_cos(q, v) for v in relevant], [_cos(q, v) for v in irrelevant]


def test_fixture_comes_from_the_pinned_local_model() -> None:
    assert RECORDED["name"] == "nomic-embed-text:v1.5"
    assert RECORDED["model"].startswith("ollama:nomic-embed-text:v1.5@")
    expected = {"search_query: " + q for q in PAIRS["queries"].values()} | {
        "search_document: " + d for docs in PAIRS["documents"].values() for d in docs
    }
    assert set(RECORDED["vectors"]) == expected


@pytest.mark.parametrize("topic", SERVICES)
def test_relevant_memories_outrank_other_services(topic: str) -> None:
    relevant, irrelevant = _scores(topic)
    assert max(relevant) > max(irrelevant)  # the best match is about the right service
    assert statistics.mean(relevant) > statistics.mean(irrelevant) + 0.05


def test_store_ranks_real_vectors_behind_hard_filters(db: Database, clock: FakeClock, user_id: str) -> None:
    store = MemoryStore(db, clock, FixtureEmbedder(RECORDED["vectors"], model=RECORDED["model"]))
    actor = Actor(user_id=user_id, session_id=make_session(db, user_id, clock.now()))
    scope_of = {
        "plumber": "plumber",
        "electrician": "electrician",
        "ac": "ac_servicing",
        "carpenter": "carpenter",
        "cleaning": "cleaning",
    }
    for topic, scope in scope_of.items():
        for doc in PAIRS["documents"][topic]:
            if doc.startswith("Note for "):
                text = doc.split(": ", 1)[1]
                assert canonical_text(NoteValue(text=text), scope) == doc  # type: ignore[arg-type]
                store.remember(actor, NoteValue(text=text), scope, statement="fixture")  # type: ignore[arg-type]
    store.remember(
        actor, ProviderValue(provider_id="PLU-IND-03", provider_name="Lokesh Plumbing"), "plumber", statement="f"
    )
    got = store.retrieve(user_id, PAIRS["queries"]["plumber"], service="plumber", min_score=MIN_SCORE)
    texts = [r.fact.text for r in got.items]
    assert texts
    assert all(t in PAIRS["documents"]["plumber"] for t in texts)  # scope filter + ranking
    assert [r.score for r in got.items] == sorted((r.score for r in got.items), reverse=True)


@pytest.mark.parametrize("topic", SERVICES)
def test_calibrated_threshold_keeps_every_relevant_memory(topic: str) -> None:
    relevant, _ = _scores(topic)
    assert min(relevant) >= MIN_SCORE


def test_calibrated_threshold_drops_unrelated_global_notes_but_keeps_the_area() -> None:
    emb = FixtureEmbedder(RECORDED["vectors"], model=RECORDED["model"])
    q = emb.embed([PAIRS["queries"]["plumber"]], kind="query")[0]
    score = {d: _cos(q, emb.embed([d], kind="document")[0]) for d in PAIRS["documents"]["neutral"]}
    assert score["Preferred area: Indiranagar"] >= MIN_SCORE  # the request is in Indiranagar
    for unrelated in ("Note: There is a friendly dog at home", "Note: Please call ten minutes before arriving"):
        assert score[unrelated] < MIN_SCORE
