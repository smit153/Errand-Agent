"""Memory store: explicit and inferred facts, confirm/correct/forget, and filtered retrieval.

Rules (DECISIONS §§7, 10, 18), backed by database checks in migration 0004:

- Explicit facts come from the user's own statements and are active once embedded.
- Inferred facts are created by code from completed, user-approved bookings, start unconfirmed
  and carry no vector. Only a separate user confirmation makes them usable. They stay "inferred".
- Value and vector change together in one short transaction. The new vector is computed before
  the transaction; if that fails the fact becomes `pending_embedding`, which is never retrieved
  or applied, so an old value can't stay in use after a correction.
- Forget clears value, vector, hash and the user's words, keeping only a tombstone
  (field, scope) so code never re-infers it.
- Retrieval hard-filters user, status, confirmation, embedding model and scope in SQL before any
  similarity is computed, then ranks by exact cosine with a fixed tie-break.

All changes for one user are serialized by a transaction-scoped advisory lock in a separate
key space from the per-session locks; no transaction here takes a session lock, so lock order
can't invert.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from errand_agent import trace
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_all, fetch_one
from errand_agent.memory.embedder import Embedder, EmbeddingUnavailableError, vector_literal
from errand_agent.memory.facts import (
    FACT_VALUE,
    AreaValue,
    EarliestTimeValue,
    MemoryFact,
    MemoryField,
    NoteValue,
    ProviderValue,
    Scope,
    canonical_text,
    check_scope,
    content_hash,
)
from errand_agent.workflow.parsing import LOCAL_TZ

Value = AreaValue | EarliestTimeValue | ProviderValue | NoteValue
Outcome = Literal["saved", "pending_embedding", "unchanged", "forgotten", "not_found", "not_allowed"]

LOCK_NAMESPACE = 0x6D656D6F  # "memo": two-key advisory locks never collide with one-key session locks
TOP_K = 6
MIN_SCORE = 0.30
INFER_WINDOW = 3  # the last three completed bookings
INFER_EARLIEST_FLOOR_HOUR = 9  # an "earliest time" below 09:00 says nothing useful
_COLUMNS = (
    "id, user_id, display_no, field, scope, value, origin, status, confirmed, revision, provenance,"
    " created_at, updated_at"
)


class Actor(BaseModel):
    """Who is changing memory. Session and run are recorded in provenance and traces."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    session_id: int | None = None
    run_id: UUID | None = None


class MemoryChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    fact: MemoryFact | None = None


class Retrieved(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact: MemoryFact
    score: float


class Retrieval(BaseModel):
    model_config = ConfigDict(frozen=True)

    query_text: str
    items: list[Retrieved] = Field(default_factory=list)
    unavailable: str | None = None  # why retrieval could not run (the turn continues without memories)


class _BookingRow(BaseModel):
    """The fields inference reads from an approved book snapshot (extra fields ignored)."""

    provider_id: str
    provider_name: str
    service: str
    slot: datetime


class MemoryStore:
    def __init__(self, db: Database, clock: Clock, embedder: Embedder) -> None:
        self._db = db
        self._clock = clock
        self.embedder = embedder

    # ================================================================== explicit facts
    def remember(self, actor: Actor, value: Value, scope: Scope, *, statement: str) -> MemoryChange:
        """Store a preference the user stated. Same field+scope (not notes) is revised in place."""
        check_scope(value, scope)
        text = canonical_text(value, scope)
        vector = self._embed_document(text)
        provenance = self._provenance(actor, source="user_statement", statement=statement)
        with self._db.txn() as conn:
            self._lock_user(conn, actor.user_id)
            existing = self._current_row(conn, actor.user_id, value, scope)
            if existing is not None:
                if (
                    existing.value == value
                    and existing.origin == "explicit"
                    and existing.status == "active"
                    and self._model_matches(conn, existing.id)
                ):
                    return MemoryChange(outcome="unchanged", fact=existing)
                fact = self._rewrite(conn, existing, value, vector, origin="explicit", provenance=provenance)
            else:
                fact = self._insert(conn, actor.user_id, value, scope, vector, origin="explicit", provenance=provenance)
            outcome: Outcome = "saved" if vector is not None else "pending_embedding"
            self._trace(conn, actor, "memory.saved" if vector is not None else "memory.pending_embedding", fact)
        return MemoryChange(outcome=outcome, fact=fact)

    # ================================================================== inferred facts
    def infer_from_bookings(self, user_id: str) -> list[MemoryFact]:
        """Code-generated, unconfirmed candidates from the user's completed, approved bookings.

        Rules: the last three bookings of one service all with the same provider → that provider
        for the service; the last three bookings overall all starting at or after 09:00 local →
        the earliest of those start times. Bookings marked as automatic suggestions are excluded.
        """
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT id, snapshot FROM operations WHERE user_id = %s AND action = 'book' AND status = 'succeeded'"
                " AND coalesce((snapshot->>'suggested')::boolean, false) = false ORDER BY created_at, id",
                (user_id,),
            )
        bookings: list[tuple[UUID, _BookingRow]] = []
        for r in rows:
            try:
                bookings.append((r["id"], _BookingRow.model_validate(r["snapshot"])))
            except ValidationError:
                continue  # not a book snapshot this rule understands: it simply contributes nothing
        candidates: list[tuple[Value, Scope, dict[str, Any]]] = []
        by_service: dict[str, list[tuple[UUID, _BookingRow]]] = {}
        for op_id, b in bookings:
            by_service.setdefault(b.service, []).append((op_id, b))
        for service, items in sorted(by_service.items()):
            last = items[-INFER_WINDOW:]
            if len(last) == INFER_WINDOW and len({b.provider_id for _, b in last}) == 1:
                b = last[-1][1]
                candidates.append(
                    (
                        ProviderValue(provider_id=b.provider_id, provider_name=b.provider_name),
                        service,  # type: ignore[arg-type]  # validated by check_scope / DB check
                        _evidence("same provider for the last 3 completed bookings of this service", last),
                    )
                )
        last_overall = bookings[-INFER_WINDOW:]
        if len(last_overall) == INFER_WINDOW:
            earliest = min(b.slot.astimezone(LOCAL_TZ).time().replace(second=0, microsecond=0) for _, b in last_overall)
            if earliest.hour >= INFER_EARLIEST_FLOOR_HOUR:
                candidates.append(
                    (
                        EarliestTimeValue(earliest=earliest),
                        "global",
                        _evidence("the last 3 completed bookings all started at or after this time", last_overall),
                    )
                )
        made: list[MemoryFact] = []
        for value, scope, evidence in candidates:
            fact = self._propose(user_id, value, scope, evidence)
            if fact is not None:
                made.append(fact)
        return made

    def _propose(self, user_id: str, value: Value, scope: Scope, evidence: dict[str, Any]) -> MemoryFact | None:
        check_scope(value, scope)
        with self._db.txn() as conn:
            self._lock_user(conn, user_id)
            if self._current_row(conn, user_id, value, scope) is not None:
                return None  # an explicit or earlier fact for this field already exists
            tombstone = fetch_one(
                conn,
                "SELECT 1 AS x FROM memories WHERE user_id = %s AND field = %s AND scope = %s AND status = 'forgotten'",
                (user_id, value.kind, scope),
            )
            if tombstone is not None:
                return None  # the user forgot this kind of fact; never infer it back
            provenance = {"source": "completed_bookings", **evidence}
            fact = self._insert(conn, user_id, value, scope, None, origin="inferred", provenance=provenance)
            self._trace(conn, Actor(user_id=user_id), "memory.inferred", fact)
        return fact

    def confirm(self, actor: Actor, display_no: int) -> MemoryChange:
        """The user's separate confirmation of an inferred candidate."""
        with self._db.txn() as conn:
            fact = self._by_number(conn, actor.user_id, display_no)
        if fact is None:
            return MemoryChange(outcome="not_found")
        if fact.status != "unconfirmed" or fact.value is None:
            return MemoryChange(outcome="not_allowed", fact=fact)
        vector = self._embed_document(fact.text)
        with self._db.txn() as conn:
            self._lock_user(conn, actor.user_id)
            again = self._by_number(conn, actor.user_id, display_no)
            if again is None or again.revision != fact.revision or again.status != "unconfirmed":
                return MemoryChange(outcome="not_allowed", fact=again)  # changed meanwhile; nothing applied
            provenance = {**fact.provenance, "confirmed_by": self._provenance(actor, source="user_confirmation")}
            updated = self._rewrite(conn, fact, fact.value, vector, origin="inferred", provenance=provenance)
            self._trace(conn, actor, "memory.confirmed", updated)
        return MemoryChange(outcome="saved" if vector is not None else "pending_embedding", fact=updated)

    # ================================================================== correction and forgetting
    def correct(self, actor: Actor, display_no: int, value: Value, *, statement: str = "") -> MemoryChange:
        """The user's correction: new value and its vector replace the old ones together."""
        with self._db.txn() as conn:
            fact = self._by_number(conn, actor.user_id, display_no)
        if fact is None:
            return MemoryChange(outcome="not_found")
        if fact.field != value.kind or fact.status not in ("active", "unconfirmed", "pending_embedding"):
            return MemoryChange(outcome="not_allowed", fact=fact)
        check_scope(value, fact.scope)
        vector = self._embed_document(canonical_text(value, fact.scope))
        with self._db.txn() as conn:
            self._lock_user(conn, actor.user_id)
            again = self._by_number(conn, actor.user_id, display_no)
            if again is None or again.revision != fact.revision:
                return MemoryChange(outcome="not_allowed", fact=again)  # compare-and-set: changed meanwhile
            provenance = self._provenance(actor, source="user_correction", statement=statement)
            updated = self._rewrite(conn, fact, value, vector, origin="explicit", provenance=provenance)
            self._trace(conn, actor, "memory.corrected" if vector is not None else "memory.pending_embedding", updated)
        return MemoryChange(outcome="saved" if vector is not None else "pending_embedding", fact=updated)

    def forget(self, actor: Actor, display_no: int) -> MemoryChange:
        with self._db.txn() as conn:
            self._lock_user(conn, actor.user_id)
            fact = self._by_number(conn, actor.user_id, display_no)
            if fact is None or fact.status in ("forgotten", "superseded"):
                return MemoryChange(outcome="not_found")
            now = self._clock.now()
            conn.execute(
                "UPDATE memories SET status = 'forgotten', value = 'null'::jsonb, embedding = NULL,"
                " embedding_model = NULL, content_hash = NULL, revision = revision + 1, updated_at = %s,"
                " provenance = %s WHERE id = %s",
                (now, Jsonb({"forgotten_at": now.isoformat(), "forgotten_by": self._who(actor)}), fact.id),
            )
            trace.record(
                conn,
                "memory.forgotten",
                {"display_no": fact.display_no, "field": fact.field, "scope": fact.scope, "origin": fact.origin},
                at=now,
                run_id=actor.run_id,
                session_id=actor.session_id,
            )
            gone = self._by_number(conn, actor.user_id, display_no)
        return MemoryChange(outcome="forgotten", fact=gone)

    def forget_field(self, actor: Actor, field: MemoryField, scope: Scope) -> MemoryChange:
        with self._db.txn() as conn:
            row = fetch_one(
                conn,
                "SELECT display_no FROM memories WHERE user_id = %s AND field = %s AND scope = %s"
                " AND status IN ('active', 'unconfirmed', 'pending_embedding') ORDER BY display_no LIMIT 1",
                (actor.user_id, field, scope),
            )
        return MemoryChange(outcome="not_found") if row is None else self.forget(actor, int(row["display_no"]))

    def retry_pending(self, user_id: str, *, limit: int = 4) -> int:
        """Embed facts that are waiting for a vector (or were embedded by another model)."""
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s AND confirmed"  # noqa: S608 - constant column list
                " AND (status = 'pending_embedding' OR (status = 'active' AND embedding_model <> %s))"
                " ORDER BY display_no LIMIT %s",
                (user_id, self.embedder.model, limit),
            )
        done = 0
        for row in rows:
            fact = _fact(row)
            if fact.value is None:
                continue
            vector = self._embed_document(fact.text)
            if vector is None:
                break  # still unavailable: leave the rest pending, never active
            with self._db.txn() as conn:
                self._lock_user(conn, user_id)
                current = self._by_number(conn, user_id, fact.display_no)
                if current is None or current.revision != fact.revision:
                    continue
                self._rewrite(conn, fact, fact.value, vector, origin=fact.origin, provenance=fact.provenance)
                self._trace(conn, Actor(user_id=user_id), "memory.embedded", fact)
            done += 1
        return done

    # ================================================================== reads
    def inspect(self, user_id: str) -> list[MemoryFact]:
        """Everything the user can manage: active, pending and unconfirmed facts, by number."""
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s"  # noqa: S608 - constant column list
                " AND status IN ('active', 'unconfirmed', 'pending_embedding') ORDER BY display_no",
                (user_id,),
            )
        return [_fact(r) for r in rows]

    def current(self, user_id: str, field: MemoryField, scope: Scope) -> MemoryFact | None:
        """The usable fact for code defaults: active, confirmed and embedded by the current model."""
        with self._db.txn() as conn:
            row = fetch_one(
                conn,
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s AND field = %s AND scope = %s"  # noqa: S608
                " AND status = 'active' AND confirmed AND embedding_model = %s ORDER BY display_no LIMIT 1",
                (user_id, field, scope, self.embedder.model),
            )
        return None if row is None else _fact(row)

    def retrieve(
        self,
        user_id: str,
        query_text: str,
        *,
        service: str | None,
        fields: tuple[MemoryField, ...] = ("area", "earliest_time", "preferred_provider", "note"),
        k: int = TOP_K,
        min_score: float = MIN_SCORE,
    ) -> Retrieval:
        try:
            query = self.embedder.embed([query_text], kind="query")[0]
        except EmbeddingUnavailableError as exc:
            return Retrieval(query_text=query_text, unavailable=str(exc))
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                # Hard filters in a materialized CTE: excluded facts are never scored at all.
                "WITH eligible AS MATERIALIZED ("  # noqa: S608 - constant column list, values bound
                f"  SELECT {_COLUMNS}, embedding FROM memories"
                "   WHERE user_id = %(user)s AND status = 'active' AND confirmed AND embedding IS NOT NULL"
                "     AND embedding_model = %(model)s AND field = ANY(%(fields)s)"
                "     AND (scope = 'global' OR scope = %(service)s)"
                "), scored AS ("
                "  SELECT *, round((1 - (embedding <=> %(q)s::vector))::numeric, 6) AS score FROM eligible"
                ") SELECT * FROM scored WHERE score >= %(min)s ORDER BY score DESC, display_no LIMIT %(k)s",
                {
                    "user": user_id,
                    "model": self.embedder.model,
                    "fields": list(fields),
                    "service": service,
                    "q": vector_literal(query),
                    "min": min_score,
                    "k": k,
                },
            )
        return Retrieval(query_text=query_text, items=[Retrieved(fact=_fact(r), score=float(r["score"])) for r in rows])

    # ================================================================== internals
    def _embed_document(self, text: str) -> list[float] | None:
        try:
            return self.embedder.embed([text], kind="document")[0]
        except EmbeddingUnavailableError:
            return None

    def _lock_user(self, conn: Conn, user_id: str) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK_NAMESPACE, user_id))

    def _current_row(self, conn: Conn, user_id: str, value: Value, scope: Scope) -> MemoryFact | None:
        if isinstance(value, NoteValue):
            row = fetch_one(
                conn,
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s AND field = 'note' AND scope = %s"  # noqa: S608
                " AND status IN ('active', 'pending_embedding') AND value = %s ORDER BY display_no LIMIT 1",
                (user_id, scope, Jsonb(value.model_dump(mode="json"))),
            )
        else:
            row = fetch_one(
                conn,
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s AND field = %s AND scope = %s"  # noqa: S608
                " AND status IN ('active', 'unconfirmed', 'pending_embedding')",
                (user_id, value.kind, scope),
            )
        return None if row is None else _fact(row)

    def _model_matches(self, conn: Conn, fact_id: UUID) -> bool:
        row = fetch_one(conn, "SELECT embedding_model FROM memories WHERE id = %s", (fact_id,))
        return row is not None and row["embedding_model"] == self.embedder.model

    def _by_number(self, conn: Conn, user_id: str, display_no: int) -> MemoryFact | None:
        row = fetch_one(
            conn,
            f"SELECT {_COLUMNS} FROM memories WHERE user_id = %s AND display_no = %s",  # noqa: S608
            (user_id, display_no),
        )
        return None if row is None else _fact(row)

    def _insert(
        self,
        conn: Conn,
        user_id: str,
        value: Value,
        scope: Scope,
        vector: list[float] | None,
        *,
        origin: Literal["explicit", "inferred"],
        provenance: dict[str, Any],
    ) -> MemoryFact:
        nxt = fetch_one(
            conn, "SELECT coalesce(max(display_no), 0) + 1 AS n FROM memories WHERE user_id = %s", (user_id,)
        )
        if nxt is None:
            raise RuntimeError("display number query returned no row")
        status = "unconfirmed" if origin == "inferred" else ("active" if vector is not None else "pending_embedding")
        text = canonical_text(value, scope)
        embedded = vector is not None and status == "active"
        now = self._clock.now()
        row = fetch_one(
            conn,
            "INSERT INTO memories (id, user_id, display_no, field, value, scope, origin, status, confirmed,"  # noqa: S608
            " provenance, embedding, embedding_model, content_hash, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s)"
            f" RETURNING {_COLUMNS}",
            (
                uuid4(),
                user_id,
                int(nxt["n"]),
                value.kind,
                Jsonb(value.model_dump(mode="json")),
                scope,
                origin,
                status,
                origin == "explicit",
                Jsonb(provenance),
                vector_literal(vector) if embedded and vector is not None else None,
                self.embedder.model if embedded else None,
                content_hash(self.embedder.model, text) if embedded else None,
                now,
                now,
            ),
        )
        if row is None:
            raise RuntimeError("memory insert returned no row")
        return _fact(row)

    def _rewrite(
        self,
        conn: Conn,
        fact: MemoryFact,
        value: Value,
        vector: list[float] | None,
        *,
        origin: Literal["explicit", "inferred"],
        provenance: dict[str, Any],
    ) -> MemoryFact:
        """Value, vector, hash, status and revision change in one statement (never a stale vector)."""
        text = canonical_text(value, fact.scope)
        status = "active" if vector is not None else "pending_embedding"
        row = fetch_one(
            conn,
            "UPDATE memories SET value = %s, origin = %s, confirmed = true, status = %s, embedding = %s::vector,"  # noqa: S608
            " embedding_model = %s, content_hash = %s, revision = revision + 1, provenance = %s, updated_at = %s"
            f" WHERE id = %s AND revision = %s RETURNING {_COLUMNS}",
            (
                Jsonb(value.model_dump(mode="json")),
                origin,
                status,
                vector_literal(vector) if vector is not None else None,
                self.embedder.model if vector is not None else None,
                content_hash(self.embedder.model, text) if vector is not None else None,
                Jsonb(provenance),
                self._clock.now(),
                fact.id,
                fact.revision,
            ),
        )
        if row is None:
            raise RuntimeError(f"memory {fact.display_no} changed during an update under the user lock")
        return _fact(row)

    def _provenance(self, actor: Actor, *, source: str, statement: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {"source": source, "at": self._clock.now().isoformat(), **self._who(actor)}
        if statement:
            out["statement"] = statement[:300]
        return out

    @staticmethod
    def _who(actor: Actor) -> dict[str, Any]:
        return {"session_id": actor.session_id, "run_id": str(actor.run_id) if actor.run_id else None}

    def _trace(self, conn: Conn, actor: Actor, kind: str, fact: MemoryFact) -> None:
        trace.record(
            conn,
            kind,
            {
                "display_no": fact.display_no,
                "field": fact.field,
                "scope": fact.scope,
                "origin": fact.origin,
                "status": fact.status,
                "revision": fact.revision,
                "text": fact.text,
            },
            at=self._clock.now(),
            run_id=actor.run_id,
            session_id=actor.session_id,
        )


def _evidence(rule: str, items: list[tuple[UUID, _BookingRow]]) -> dict[str, Any]:
    return {"rule": rule, "operation_ids": [str(op) for op, _ in items], "confidence": f"{len(items)} of {len(items)}"}


def _fact(row: Any) -> MemoryFact:
    data = {k: row[k] for k in _COLUMNS.replace(" ", "").split(",")}
    data["value"] = None if data["value"] is None else FACT_VALUE.validate_python(data["value"])
    return MemoryFact.model_validate(data)
