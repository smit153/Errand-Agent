"""Stage 5a: memory lifecycle invariants (PDF §3 persistent memory and context; DECISIONS §§7, 10, 18).

Written before the store. The rules under test:
- forgotten facts keep nothing and never reach retrieval or code defaults, and are not re-inferred;
- unconfirmed inferred facts never reach retrieval or code defaults until the user confirms;
- a correction replaces value and vector together; if the new vector can't be made, the fact is
  pending (unused), never left active with a stale vector;
- the database rejects illegal states even when application code is bypassed;
- hard filters run before similarity, so an excluded fact can never rank.
"""

from __future__ import annotations

from datetime import time, timedelta

import psycopg
import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.memory.embedder import (
    DIMENSIONS,
    EmbeddingUnavailableError,
    FixtureEmbedder,
    basis,
    validate_vector,
    vector_literal,
)
from errand_agent.memory.facts import AreaValue, EarliestTimeValue, NoteValue, ProviderValue, canonical_text
from errand_agent.memory.store import Actor, MemoryStore
from tests.conftest import T0, make_session
from tests.helpers import count
from tests.memory_helpers import HashEmbedder, succeeded_booking

pytestmark = pytest.mark.invariant


@pytest.fixture
def emb() -> HashEmbedder:
    return HashEmbedder()


@pytest.fixture
def store(db: Database, clock: FakeClock, emb: HashEmbedder) -> MemoryStore:
    return MemoryStore(db, clock, emb)


@pytest.fixture
def actor(db: Database, user_id: str, clock: FakeClock) -> Actor:
    return Actor(user_id=user_id, session_id=make_session(db, user_id, clock.now()))


def _vector(db: Database, display_no: int, user: str = "user-demo") -> str | None:
    with db.txn() as conn:
        row = conn.execute(
            "SELECT embedding::text AS v FROM memories WHERE user_id = %s AND display_no = %s", (user, display_no)
        ).fetchone()
    assert row is not None
    return None if row["v"] is None else str(row["v"])


# ---------------------------------------------------------------- explicit facts
def test_explicit_fact_is_active_embedded_and_retrievable(store: MemoryStore, actor: Actor, emb: HashEmbedder) -> None:
    emb.align_query(canonical_text(AreaValue(area="indiranagar"), "global"))
    change = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="remember my area")
    assert change.outcome == "saved"
    fact = change.fact
    assert fact is not None
    assert (fact.origin, fact.status, fact.confirmed, fact.revision, fact.display_no) == (
        "explicit",
        "active",
        True,
        1,
        1,
    )
    assert store.current(actor.user_id, "area", "global") == fact
    got = store.retrieve(actor.user_id, fact.text, service=None)
    assert [r.fact.id for r in got.items] == [fact.id]
    assert got.items[0].score == pytest.approx(1.0)


def test_same_value_again_is_unchanged_and_new_value_revises_in_place(store: MemoryStore, actor: Actor) -> None:
    first = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s1").fact
    assert store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s2").outcome == "unchanged"
    second = store.remember(actor, AreaValue(area="koramangala"), "global", statement="s3").fact
    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.display_no == first.display_no
    assert second.revision == 2
    assert second.value == AreaValue(area="koramangala")
    assert [f.id for f in store.inspect(actor.user_id)] == [first.id]


def test_database_allows_only_one_current_fact_per_field_and_scope(
    store: MemoryStore, actor: Actor, db: Database
) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    with pytest.raises(psycopg.errors.UniqueViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, user_id, display_no, field, value, scope, origin, status, confirmed,"
            " provenance, created_at, updated_at) VALUES (gen_random_uuid(), %s, 99, 'area',"
            " '{\"kind\": \"area\", \"area\": \"whitefield\"}', 'global', 'inferred', 'unconfirmed', false, '{}',"
            " now(), now())",
            (actor.user_id,),
        )


def test_notes_can_coexist_and_are_scoped(store: MemoryStore, actor: Actor) -> None:
    a = store.remember(actor, NoteValue(text="Ring the bell twice"), "global", statement="s").fact
    b = store.remember(actor, NoteValue(text="The AC unit is on the balcony"), "ac_servicing", statement="s").fact
    assert a is not None
    assert b is not None
    assert a.display_no != b.display_no
    assert store.remember(actor, NoteValue(text="Ring the bell twice"), "global", statement="s").outcome == "unchanged"
    plumber = store.retrieve(actor.user_id, b.text, service="plumber", min_score=-1.0)
    assert [r.fact.id for r in plumber.items] == [a.id]  # the AC note is out of scope for a plumber query


def test_scope_rules_are_enforced(store: MemoryStore, actor: Actor) -> None:
    with pytest.raises(ValueError, match="global"):
        store.remember(actor, AreaValue(area="indiranagar"), "plumber", statement="s")
    with pytest.raises(ValueError, match="one service"):
        store.remember(actor, ProviderValue(provider_id="PLU-IND-03", provider_name="Lokesh"), "global", statement="s")


# ---------------------------------------------------------------- forgetting
def test_forget_clears_value_vector_and_hash_and_hides_everywhere(
    store: MemoryStore, actor: Actor, db: Database
) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="my area is Indiranagar").fact
    assert fact is not None
    change = store.forget(actor, fact.display_no)
    assert change.outcome == "forgotten"
    with db.txn() as conn:
        row = conn.execute(
            "SELECT status, value, embedding, content_hash, embedding_model, provenance FROM memories WHERE id = %s",
            (fact.id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "forgotten"
    assert row["value"] is None
    assert row["embedding"] is None
    assert row["content_hash"] is None
    assert row["embedding_model"] is None
    assert "Indiranagar" not in str(row["provenance"])  # the user's words are not kept either
    assert store.current(actor.user_id, "area", "global") is None
    assert store.inspect(actor.user_id) == []
    assert store.retrieve(actor.user_id, fact.text, service=None, min_score=-1.0).items == []
    assert store.forget(actor, fact.display_no).outcome == "not_found"


def test_database_rejects_a_forgotten_fact_that_keeps_content(db: Database, user_id: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, user_id, display_no, field, value, scope, origin, status, confirmed,"
            " provenance, created_at, updated_at) VALUES (gen_random_uuid(), %s, 1, 'area',"
            " '{\"kind\": \"area\", \"area\": \"whitefield\"}', 'global', 'explicit', 'forgotten', true, '{}',"
            " now(), now())",
            (user_id,),
        )


def test_forgetting_then_remembering_again_starts_a_new_fact(store: MemoryStore, actor: Actor) -> None:
    old = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert old is not None
    store.forget(actor, old.display_no)
    new = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert new is not None
    assert new.id != old.id
    assert new.display_no != old.display_no


# ---------------------------------------------------------------- inferred facts
def _three_bookings(db: Database, user: str, session: int, *, provider: str = "ELE-IND-01", hour: int = 11) -> None:
    for day in range(3):
        slot = (T0 - timedelta(days=30 - 7 * day)).replace(hour=hour - 6, minute=30)  # hour:00 IST (UTC+5:30)
        succeeded_booking(
            db,
            session_id=session,
            user_id=user,
            provider_id=provider,
            provider_name="Jatin Electricals",
            service="electrician",
            slot=slot,
            now=T0,
        )


def test_inferred_candidates_are_unconfirmed_and_never_used(store: MemoryStore, actor: Actor, db: Database) -> None:
    assert actor.session_id is not None
    _three_bookings(db, actor.user_id, actor.session_id)
    made = store.infer_from_bookings(actor.user_id)
    fields = sorted((f.field, f.scope) for f in made)
    assert fields == [("earliest_time", "global"), ("preferred_provider", "electrician")]
    for f in made:
        assert (f.origin, f.status, f.confirmed) == ("inferred", "unconfirmed", False)
        assert _vector(db, f.display_no) is None
        assert f.provenance["rule"]
        assert f.provenance["operation_ids"]
    assert store.current(actor.user_id, "preferred_provider", "electrician") is None
    assert store.current(actor.user_id, "earliest_time", "global") is None
    query = canonical_text(ProviderValue(provider_id="ELE-IND-01", provider_name="Jatin Electricals"), "electrician")
    assert store.retrieve(actor.user_id, query, service="electrician", min_score=-1.0).items == []
    assert store.infer_from_bookings(actor.user_id) == []  # idempotent


def test_two_bookings_or_mixed_providers_infer_no_provider(store: MemoryStore, actor: Actor, db: Database) -> None:
    assert actor.session_id is not None
    for day, provider in enumerate(["ELE-IND-01", "ELE-IND-02", "ELE-IND-01"]):
        slot = (T0 - timedelta(days=20 - 5 * day)).replace(hour=3, minute=30)  # 09:00 IST
        succeeded_booking(
            db,
            session_id=actor.session_id,
            user_id=actor.user_id,
            provider_id=provider,
            provider_name=provider,
            service="electrician",
            slot=slot,
            now=T0,
        )
    made = store.infer_from_bookings(actor.user_id)
    assert [(f.field, f.value) for f in made] == [("earliest_time", EarliestTimeValue(earliest=time(9, 0)))]


def test_database_rejects_unconfirmed_inferred_fact_marked_active(db: Database, user_id: str) -> None:
    v = vector_literal(basis((0, 1.0)))
    with pytest.raises(psycopg.errors.CheckViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, user_id, display_no, field, value, scope, origin, status, confirmed,"
            " provenance, embedding, embedding_model, content_hash, created_at, updated_at) VALUES"
            ' (gen_random_uuid(), %s, 1, \'area\', \'{"kind": "area", "area": "whitefield"}\', \'global\','
            " 'inferred', 'active', false, '{}', %s::vector, 'm', 'h', now(), now())",
            (user_id, v),
        )


def test_confirm_promotes_an_inferred_candidate_and_keeps_it_inferred(
    store: MemoryStore, actor: Actor, db: Database
) -> None:
    assert actor.session_id is not None
    _three_bookings(db, actor.user_id, actor.session_id)
    candidate = next(f for f in store.infer_from_bookings(actor.user_id) if f.field == "preferred_provider")
    change = store.confirm(actor, candidate.display_no)
    assert change.outcome == "saved"
    assert change.fact is not None
    assert (change.fact.origin, change.fact.status, change.fact.confirmed) == ("inferred", "active", True)
    assert store.current(actor.user_id, "preferred_provider", "electrician") == change.fact
    assert store.confirm(actor, candidate.display_no).outcome == "not_allowed"  # already confirmed


def test_explicit_fact_overrides_an_inferred_candidate(store: MemoryStore, actor: Actor, db: Database) -> None:
    assert actor.session_id is not None
    _three_bookings(db, actor.user_id, actor.session_id, hour=11)
    candidate = next(f for f in store.infer_from_bookings(actor.user_id) if f.field == "earliest_time")
    stated = store.remember(actor, EarliestTimeValue(earliest=time(9, 30)), "global", statement="s").fact
    assert stated is not None
    assert stated.id == candidate.id
    assert (stated.origin, stated.status, stated.confirmed) == ("explicit", "active", True)
    assert stated.value == EarliestTimeValue(earliest=time(9, 30))


def test_forgotten_inference_is_not_inferred_again(store: MemoryStore, actor: Actor, db: Database) -> None:
    assert actor.session_id is not None
    _three_bookings(db, actor.user_id, actor.session_id)
    for f in store.infer_from_bookings(actor.user_id):
        store.forget(actor, f.display_no)
    assert store.infer_from_bookings(actor.user_id) == []


def test_bookings_of_other_users_never_infer(store: MemoryStore, actor: Actor, db: Database, clock: FakeClock) -> None:
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
    other = make_session(db, "user-other", clock.now())
    _three_bookings(db, "user-other", other)
    assert store.infer_from_bookings(actor.user_id) == []


# ---------------------------------------------------------------- correction and embedding failure
def test_correct_replaces_value_and_vector_together(store: MemoryStore, actor: Actor, db: Database) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    before = _vector(db, fact.display_no)
    change = store.correct(actor, fact.display_no, AreaValue(area="whitefield"))
    assert change.outcome == "saved"
    assert change.fact is not None
    assert change.fact.revision == 2
    assert change.fact.value == AreaValue(area="whitefield")
    after = _vector(db, fact.display_no)
    assert after is not None
    assert after != before
    expected = store.embedder.embed([change.fact.text], kind="document")[0]
    got = [float(x) for x in after.strip("[]").split(",")]
    assert got == pytest.approx(expected, abs=1e-6)  # the stored vector is the new text's vector


def test_correction_when_embedder_is_down_leaves_nothing_active(
    store: MemoryStore, actor: Actor, emb: HashEmbedder, db: Database
) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    emb.available = False
    change = store.correct(actor, fact.display_no, AreaValue(area="whitefield"))
    assert change.outcome == "pending_embedding"
    assert change.fact is not None
    assert change.fact.status == "pending_embedding"
    assert change.fact.value == AreaValue(area="whitefield")
    assert _vector(db, fact.display_no) is None
    assert store.current(actor.user_id, "area", "global") is None  # neither the old nor the new value is used
    emb.available = True
    assert store.retrieve(actor.user_id, fact.text, service=None, min_score=-1.0).items == []
    assert store.retry_pending(actor.user_id) == 1
    current = store.current(actor.user_id, "area", "global")
    assert current is not None
    assert current.value == AreaValue(area="whitefield")


def test_correct_rejects_a_value_of_another_field(store: MemoryStore, actor: Actor) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    assert store.correct(actor, fact.display_no, EarliestTimeValue(earliest=time(10, 0))).outcome == "not_allowed"


def test_numbers_belong_to_their_user(store: MemoryStore, actor: Actor, db: Database, clock: FakeClock) -> None:
    fact = store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
    intruder = Actor(user_id="user-other", session_id=make_session(db, "user-other", clock.now()))
    assert store.forget(intruder, fact.display_no).outcome == "not_found"
    assert store.correct(intruder, fact.display_no, AreaValue(area="whitefield")).outcome == "not_found"
    assert store.current(actor.user_id, "area", "global") == fact


# ---------------------------------------------------------------- retrieval: hard filters first
def test_hard_filters_exclude_facts_that_would_score_highest(db: Database, clock: FakeClock, user_id: str) -> None:
    emb = FixtureEmbedder({})
    store = MemoryStore(db, clock, emb)
    actor = Actor(user_id=user_id, session_id=make_session(db, user_id, clock.now()))
    query = "book plumber in Indiranagar"
    emb.add(query, "query", basis((0, 1.0)))

    def note(text: str, scope: str, vec: list[float]) -> int:
        emb.add(canonical_text(NoteValue(text=text), scope), "document", vec)  # type: ignore[arg-type]
        f = store.remember(actor, NoteValue(text=text), scope, statement=text).fact  # type: ignore[arg-type]
        assert f is not None
        return f.display_no

    relevant = note("Kitchen tap is the old brass one", "plumber", basis((0, 0.9), (1, 0.1)))
    weak = note("Prefers WhatsApp messages", "global", basis((0, 0.2), (2, 1.0)))
    forgotten = note("Forgotten plumber detail", "plumber", basis((0, 1.0)))
    other_service = note("AC remote is in the drawer", "ac_servicing", basis((0, 1.0)))
    store.forget(actor, forgotten)
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-other')")
    other = Actor(user_id="user-other", session_id=make_session(db, "user-other", clock.now()))
    emb.add(canonical_text(NoteValue(text="Other user's plumber note"), "plumber"), "document", basis((0, 1.0)))
    store.remember(other, NoteValue(text="Other user's plumber note"), "plumber", statement="s")

    got = store.retrieve(user_id, query, service="plumber")
    assert [r.fact.display_no for r in got.items] == [relevant]  # weak is below 0.30
    assert got.items[0].score == round(got.items[0].score, 6)
    everything = store.retrieve(user_id, query, service="plumber", min_score=-1.0)
    assert [r.fact.display_no for r in everything.items] == [relevant, weak]
    assert other_service not in [r.fact.display_no for r in everything.items]


def test_ties_break_by_number_and_k_limits(db: Database, clock: FakeClock, user_id: str) -> None:
    emb = FixtureEmbedder({})
    store = MemoryStore(db, clock, emb)
    actor = Actor(user_id=user_id, session_id=make_session(db, user_id, clock.now()))
    emb.add("q", "query", basis((0, 1.0)))
    numbers = []
    for i in range(8):
        text = f"note {i}"
        emb.add(canonical_text(NoteValue(text=text), "global"), "document", basis((0, 1.0), (1 + i, 0.5)))
        f = store.remember(actor, NoteValue(text=text), "global", statement=text).fact
        assert f is not None
        numbers.append(f.display_no)
    got = store.retrieve(user_id, "q", service=None, k=6)
    assert [r.fact.display_no for r in got.items] == numbers[:6]


def test_facts_embedded_with_another_model_are_not_compared(db: Database, clock: FakeClock, user_id: str) -> None:
    old = HashEmbedder(model="old-model")
    actor = Actor(user_id=user_id, session_id=make_session(db, user_id, clock.now()))
    fact = MemoryStore(db, clock, old).remember(actor, AreaValue(area="indiranagar"), "global", statement="s").fact
    assert fact is not None
    new_store = MemoryStore(db, clock, HashEmbedder(model="new-model"))
    assert new_store.retrieve(user_id, fact.text, service=None, min_score=-1.0).items == []
    assert new_store.current(user_id, "area", "global") is None  # stale model → re-embed before use
    assert new_store.retry_pending(user_id) == 1
    assert new_store.current(user_id, "area", "global") is not None


def test_query_embedding_failure_is_explicit(store: MemoryStore, actor: Actor, emb: HashEmbedder) -> None:
    store.remember(actor, AreaValue(area="indiranagar"), "global", statement="s")
    emb.available = False
    got = store.retrieve(actor.user_id, "anything", service=None)
    assert got.items == []
    assert got.unavailable is not None


def test_every_change_is_traced_without_forgotten_text(store: MemoryStore, actor: Actor, db: Database) -> None:
    fact = store.remember(actor, NoteValue(text="secret gate code 4321"), "global", statement="s").fact
    assert fact is not None
    store.forget(actor, fact.display_no)
    assert count(db, "SELECT count(*) AS n FROM trace_events WHERE kind = 'memory.saved'") == 1
    assert count(db, "SELECT count(*) AS n FROM trace_events WHERE kind = 'memory.forgotten'") == 1
    assert (
        count(
            db,
            "SELECT count(*) AS n FROM trace_events WHERE kind = 'memory.forgotten' AND payload::text LIKE '%%4321%%'",
        )
        == 0
    )


# ---------------------------------------------------------------- embedder output is untrusted
@pytest.mark.parametrize(
    "raw",
    [[0.0] * DIMENSIONS, [1.0] * (DIMENSIONS - 1), [float("nan")] + [0.0] * (DIMENSIONS - 1), "x", [True] * DIMENSIONS],
)
def test_invalid_vectors_are_rejected(raw: object) -> None:
    with pytest.raises(EmbeddingUnavailableError):
        validate_vector(raw)


def test_fixture_embedder_never_invents_vectors() -> None:
    emb = FixtureEmbedder({})
    with pytest.raises(EmbeddingUnavailableError, match="no fixture vector"):
        emb.embed(["unknown"], kind="query")
