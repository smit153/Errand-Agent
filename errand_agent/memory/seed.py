"""Seed data for the context-management evaluation (PDF §3: at least 50 stored memories and a
long prior conversation; DECISIONS §10, E21).

The seed is deliberately hostile: most rows must never reach the model, and those rows are
written to be *more* relevant to a plumbing request than any legitimate note, so only the hard
filters (not luck in ranking) keep them out. Every row that is not
eligible (forgotten before it was forgotten, superseded, pending, unconfirmed, embedded by
another model, or belonging to another user) and every prior-conversation message carries the
marker `CANARY`, so a test can scan every model request for it. Eligible rows are a mix of
relevant and irrelevant facts, so ranking and the byte cap are exercised as well as filtering.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent import trace
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.memory.embedder import vector_literal
from errand_agent.memory.facts import (
    AreaValue,
    EarliestTimeValue,
    NoteValue,
    ProviderValue,
    Scope,
    canonical_text,
    content_hash,
)
from errand_agent.memory.store import Actor, MemoryStore

CANARY = "CANARY"
OTHER_USER = "user-seed-other"

_NOTES: dict[Scope, list[str]] = {
    "global": [
        "Please call ten minutes before arriving",
        "There is a friendly dog at home",
        "Parking is available in the basement",
        "Prefers paying by UPI after the job",
    ],
    "plumber": [
        "Kitchen tap is a single-lever mixer from 2019",
        "Main water valve is under the kitchen sink",
        "Bathroom flush leaks slowly",
        "Water pressure is low in the mornings",
        "Prefers plumbers who bring spare tap washers",
        "Two tap repairs already this year",
        "The geyser is in the utility area next to the washing machine",
    ],
    "electrician": [
        "Old wiring in the bedroom",
        "The MCB box is behind the main door",
        "Ceiling fan in the hall wobbles",
        "Inverter battery was replaced last year",
        "Prefers switches from the same brand as before",
    ],
    "ac_servicing": [
        "Split AC unit is on the balcony",
        "AC remote is in the drawer",
        "Gas refill was done in March",
        "Filters need cleaning every season",
        "Bedroom AC drips water",
    ],
    "carpenter": [
        "Wardrobe hinge is loose",
        "Kitchen cabinet door does not close",
        "Prefers teak finish",
        "Bookshelf needs wall anchoring",
    ],
    "cleaning": [
        "Sofa is fabric, not leather",
        "Use mild products because of the dog",
        "Deep clean the kitchen chimney",
        "Balcony glass is hard to reach",
    ],
}


class SeedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_memories: int
    other_user_memories: int
    by_status: dict[str, int]
    prior_sessions: int
    prior_messages: int


def seed_memories(db: Database, clock: Clock, store: MemoryStore, user_id: str) -> SeedReport:
    """At least 50 memories for `user_id` across every lifecycle state, plus another user's."""
    actor = Actor(user_id=user_id)
    for value, typed_scope in [
        (AreaValue(area="indiranagar"), "global"),
        (EarliestTimeValue(earliest=time(9, 0)), "global"),
        (ProviderValue(provider_id="PLU-IND-03", provider_name="Lokesh Plumbing"), "plumber"),
        (ProviderValue(provider_id="ELE-IND-01", provider_name="Jatin Electricals"), "electrician"),
        (ProviderValue(provider_id="ACS-IND-01", provider_name="Harish Cooling Care"), "ac_servicing"),
        (EarliestTimeValue(earliest=time(10, 0)), "cleaning"),
    ]:
        store.remember(actor, value, typed_scope, statement="seeded explicit preference")  # type: ignore[arg-type]
    for note_scope, notes in _NOTES.items():
        for text in notes:
            store.remember(actor, NoteValue(text=text), note_scope, statement="seeded note")
    # Active but scoped to other services, while worded like plumbing: only the scope filter keeps them out.
    for i, other_scope in enumerate(("cleaning", "carpenter")):
        store.remember(
            actor,
            NoteValue(text=f"{CANARY} out of scope: plumber tap leak, sink pipe and water valve {i}"),
            other_scope,  # type: ignore[arg-type]
            statement="seeded note",
        )
    # Code-inferred suggestions that the user never confirmed: unusable, never in context.
    store.propose_inferred(user_id, EarliestTimeValue(earliest=time(11, 0)), "plumber", {"rule": "seed"})
    store.propose_inferred(user_id, EarliestTimeValue(earliest=time(12, 0)), "carpenter", {"rule": "seed"})
    store.propose_inferred(
        user_id,
        ProviderValue(provider_id="CAR-IND-01", provider_name=f"{CANARY} Unconfirmed Carpentry"),
        "carpenter",
        {"rule": "seed"},
    )
    store.propose_inferred(
        user_id,
        ProviderValue(provider_id="CLN-IND-01", provider_name=f"{CANARY} Unconfirmed Cleaners"),
        "cleaning",
        {"rule": "seed"},
    )
    # Forgotten: stored with a canary, then forgotten (value, vector and words are cleared).
    for i in range(6):
        scope: Scope = "plumber" if i % 2 else "global"
        text = f"{CANARY} forgotten: plumber for the kitchen tap leak, water valve and pipe {i}"
        change = store.remember(actor, NoteValue(text=text), scope, statement="x")
        if change.fact is not None:
            store.forget(actor, change.fact.display_no)
    with db.txn() as conn:
        for i in range(5):  # obsolete, superseded facts
            _raw_note(
                conn,
                clock,
                store,
                user_id,
                text=f"{CANARY} superseded: plumber tap washer leak under the sink pipe {i}",
                scope="plumber",
                status="superseded",
            )
        for i in range(3):  # waiting for a vector
            _raw_note(
                conn,
                clock,
                store,
                user_id,
                text=f"{CANARY} pending: plumber pipe leak, tap and low water pressure {i}",
                scope="plumber",
                status="pending_embedding",
            )
        for i in range(2):  # embedded by an older model: never compared with today's vectors
            _raw_note(
                conn,
                clock,
                store,
                user_id,
                text=f"{CANARY} old model: plumber tap leak, sink pipe and water valve {i}",
                scope="plumber",
                status="active",
                model="old-model",
            )
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (OTHER_USER,))
    other = Actor(user_id=OTHER_USER)
    for i in range(6):
        text = f"{CANARY} another user's plumber tap leak, pipe and water valve {i}"
        store.remember(other, NoteValue(text=text), "plumber", statement="x")
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM memories WHERE user_id = %s GROUP BY status", (user_id,)
        ).fetchall()
        others = fetch_one(conn, "SELECT count(*) AS n FROM memories WHERE user_id = %s", (OTHER_USER,))
    by_status = {str(r["status"]): int(r["n"]) for r in rows}
    return SeedReport(
        user_memories=sum(by_status.values()),
        other_user_memories=int(others["n"]) if others else 0,
        by_status=by_status,
        prior_sessions=0,
        prior_messages=0,
    )


def seed_prior_conversation(
    db: Database, clock: Clock, user_id: str, *, sessions: int = 6, turns_per_session: int = 20
) -> int:
    """A long earlier conversation, kept only as audit traces of closed sessions. Returns messages written."""
    written = 0
    start = clock.now() - timedelta(days=60)
    with db.txn() as conn:
        for s in range(sessions):
            at = start + timedelta(days=7 * s)
            row = fetch_one(
                conn,
                "INSERT INTO sessions (user_id, state, request, created_at, updated_at)"
                " VALUES (%s, 'completed', %s, %s, %s) RETURNING id",
                (user_id, Jsonb({"intent": "book", "service": "plumber", "area": "koramangala"}), at, at),
            )
            if row is None:
                raise RuntimeError("could not create a prior session")
            for t in range(turns_per_session):
                when = at + timedelta(minutes=t)
                trace.record(
                    conn,
                    "agent.user_message",
                    {"text": f"{CANARY} prior chat {s}.{t}: my neighbour's plumber charged ₹9,999 last time"},
                    at=when,
                    session_id=int(row["id"]),
                )
                trace.record(
                    conn,
                    "agent.reply",
                    {"messages": [f"{CANARY} prior reply {s}.{t}"], "state": "collecting", "exit": "replied"},
                    at=when,
                    session_id=int(row["id"]),
                )
                written += 2
    return written


def _raw_note(
    conn: Conn,
    clock: Clock,
    store: MemoryStore,
    user_id: str,
    *,
    text: str,
    scope: Scope,
    status: str,
    model: str | None = None,
) -> None:
    """Rows the store never produces this way (superseded, old-model), inserted for the evaluation."""
    value = NoteValue(text=text)
    nxt = fetch_one(conn, "SELECT coalesce(max(display_no), 0) + 1 AS n FROM memories WHERE user_id = %s", (user_id,))
    if nxt is None:
        raise RuntimeError("display number query returned no row")
    vector: Any = None
    if model is not None:
        vector = vector_literal(store.embedder.embed([canonical_text(value, scope)], kind="document")[0])
    now = clock.now()
    conn.execute(
        "INSERT INTO memories (id, user_id, display_no, field, value, scope, origin, status, confirmed, provenance,"
        " embedding, embedding_model, content_hash, created_at, updated_at)"
        " VALUES (%s, %s, %s, 'note', %s, %s, 'explicit', %s, true, %s, %s::vector, %s, %s, %s, %s)",
        (
            uuid4(),
            user_id,
            int(nxt["n"]),
            Jsonb(value.model_dump(mode="json")),
            scope,
            status,
            Jsonb({"source": "seed"}),
            vector,
            model,
            content_hash(model, canonical_text(value, scope)) if model else None,
            now,
            now,
        ),
    )
