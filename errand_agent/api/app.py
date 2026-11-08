"""Local HTTP API (stage 8; DECISIONS §8): a thin layer over the tested engine.

Loopback only and one local user: every session-scoped request is checked against that user
(404 otherwise). Chat messages carry a client message ID: a retried request gets the stored
reply instead of a second turn, and a different text under the same ID is refused. The agent
still owns all state, approvals and execution; display receipts flow through unchanged, and
approval cards delivered as notifications are receipted when fetched.

Demo controls steer the mocks (next booking outcome, a provider cancellation) for the recorded
demo; they exist only when explicitly enabled.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from errand_agent.clock import Clock
from errand_agent.db import Database, fetch_all, fetch_one
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.tools.errors import ToolRejectedError
from errand_agent.workers.outbox import Outbox
from errand_agent.workflow.agent import Agent
from errand_agent.workflow.approval import ProposalBlockedError

from .tracefmt import line

STALE_IN_FLIGHT = timedelta(seconds=360)  # longer than a run may take; then a retry may run the turn again


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    client_message_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    receipt_id: UUID | None = None


class MessageOut(BaseModel):
    messages: list[str]
    state: str
    receipt_id: UUID | None = None
    replayed: bool = False


class SessionOut(BaseModel):
    session_id: int
    state: str


class CardOut(BaseModel):
    text: str
    receipt_id: UUID


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    session_id: int | None
    text: str
    receipt_id: UUID | None


class AckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(max_length=100)


class TraceLine(BaseModel):
    id: int
    session_id: int | None
    kind: str
    tag: str
    line: str
    payload: dict[str, Any] | None = None


class NextBookingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "slot_taken", "timeout_booked", "timeout_lost"]


class ProviderCancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_id: Annotated[str, StringConstraints(pattern=r"^BK-[A-Z0-9]{4,12}$")]


def create_app(
    *,
    db: Database,
    clock: Clock,
    agent: Agent,
    outbox: Outbox,
    backend: MockBackend,
    user_id: str,
    demo_controls: bool = False,
    info: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI(title="Errand Agent (local)", version="0.1.0", docs_url=None, redoc_url=None)

    def own_session(session_id: int) -> str:
        with db.txn() as conn:
            row = fetch_one(conn, "SELECT user_id, state FROM sessions WHERE id = %s", (session_id,))
        if row is None or row["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="no such conversation")
        return str(row["state"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "user": user_id, **(info or {})}

    @app.post("/sessions", status_code=201)
    def new_session() -> SessionOut:
        with db.txn() as conn:
            row = fetch_one(
                conn,
                "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s) RETURNING id, state",
                (user_id, clock.now(), clock.now()),
            )
        if row is None:
            raise HTTPException(status_code=500, detail="could not create a conversation")
        return SessionOut(session_id=int(row["id"]), state=str(row["state"]))

    @app.get("/sessions")
    def sessions() -> list[SessionOut]:
        with db.txn() as conn:
            rows = fetch_all(
                conn, "SELECT id, state FROM sessions WHERE user_id = %s ORDER BY id DESC LIMIT 50", (user_id,)
            )
        return [SessionOut(session_id=int(r["id"]), state=str(r["state"])) for r in rows]

    @app.post("/sessions/{session_id}/messages")
    def send(session_id: int, body: MessageIn) -> MessageOut:
        own_session(session_id)
        replay = _begin_request(db, session_id, body, clock.now())
        if replay is not None:
            return replay
        reply = agent.handle(session_id, user_id, body.text, body.receipt_id)
        out = MessageOut(messages=reply.messages, state=reply.state, receipt_id=reply.receipt_id)
        with db.txn() as conn:
            conn.execute(
                "UPDATE api_requests SET response = %s WHERE session_id = %s AND client_message_id = %s",
                (Jsonb(out.model_dump(mode="json", exclude={"replayed"})), session_id, body.client_message_id),
            )
        return out

    @app.get("/sessions/{session_id}/card")
    def pending_card(session_id: int) -> CardOut:
        """Show the pending approval card again (e.g. after reconnecting) and issue a fresh receipt."""
        own_session(session_id)
        with db.txn() as conn:
            row = fetch_one(
                conn, "SELECT id FROM proposals WHERE session_id = %s AND status = 'pending'", (session_id,)
            )
        if row is None:
            raise HTTPException(status_code=404, detail="no card is waiting for approval")
        try:
            receipt = agent.gate.display(session_id, row["id"])
        except ProposalBlockedError as exc:
            raise HTTPException(status_code=404, detail="no card is waiting for approval") from exc
        return CardOut(text=receipt.text, receipt_id=receipt.id)

    @app.get("/sessions/{session_id}/trace")
    def session_trace(session_id: int, after: Annotated[int, Query(ge=0)] = 0, raw: bool = False) -> list[TraceLine]:
        own_session(session_id)
        return _trace(db, user_id, after=after, raw=raw, session_id=session_id)

    @app.get("/trace")
    def user_trace(after: Annotated[int, Query(ge=0)] = 0, raw: bool = False) -> list[TraceLine]:
        return _trace(db, user_id, after=after, raw=raw, session_id=None)

    @app.get("/notifications")
    def notifications() -> list[NotificationOut]:
        return [
            NotificationOut(id=n.id, kind=n.kind, session_id=n.session_id, text=n.text, receipt_id=n.receipt_id)
            for n in outbox.fetch(user_id)
        ]

    @app.post("/notifications/ack")
    def ack(body: AckIn) -> dict[str, int]:
        return {"acked": outbox.ack(user_id, body.ids)}

    if demo_controls:
        _add_demo_routes(app, db=db, backend=backend, user_id=user_id)

    return app


def _trace(db: Database, user_id: str, *, after: int, raw: bool, session_id: int | None) -> list[TraceLine]:
    with db.txn() as conn:
        rows = fetch_all(
            conn,
            "SELECT t.id, t.at, t.session_id, t.kind, t.payload FROM trace_events t"
            " JOIN sessions s ON s.id = t.session_id WHERE s.user_id = %s AND t.id > %s"
            " AND (%s::bigint IS NULL OR t.session_id = %s) ORDER BY t.id LIMIT 500",
            (user_id, after, session_id, session_id),
        )
    out = []
    for r in rows:
        tag, text = line(int(r["id"]), r["at"], r["session_id"], r["kind"], r["payload"])
        out.append(
            TraceLine(
                id=int(r["id"]),
                session_id=r["session_id"],
                kind=r["kind"],
                tag=tag,
                line=text,
                payload=r["payload"] if raw else None,
            )
        )
    return out


def _add_demo_routes(app: FastAPI, *, db: Database, backend: MockBackend, user_id: str) -> None:
    """Test/demo controls that steer the mocks; registered only when explicitly enabled."""

    @app.post("/demo/next-booking")
    def next_booking(body: NextBookingIn) -> dict[str, str]:
        backend.configure(ScenarioConfig(book_outcomes=[body.outcome]))
        return {"next_booking": body.outcome}

    @app.post("/demo/provider-cancel")
    def provider_cancel(body: ProviderCancelIn) -> dict[str, Any]:
        with db.txn() as conn:
            row = fetch_one(conn, "SELECT user_id FROM mock_bookings WHERE id = %s", (body.booking_id,))
        if row is None or row["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="no such booking")
        try:
            events = backend.simulate_provider_cancellation(body.booking_id)
        except ToolRejectedError as exc:
            raise HTTPException(status_code=409, detail="booking is not confirmed") from exc
        return {"cancelled_by_provider": body.booking_id, "events": events}

    @app.get("/demo/bookings")
    def bookings() -> list[dict[str, Any]]:
        with db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT id, provider_id, service, slot, status FROM mock_bookings WHERE user_id = %s ORDER BY slot",
                (user_id,),
            )
        return [{**dict(r), "slot": r["slot"].isoformat()} for r in rows]


def _begin_request(db: Database, session_id: int, body: MessageIn, now: Any) -> MessageOut | None:
    """Record a new request, or return the stored reply of a retried one (409 while in flight)."""
    digest = hashlib.sha256(body.text.encode("utf-8")).hexdigest()
    with db.txn() as conn:
        inserted = conn.execute(
            "INSERT INTO api_requests (session_id, client_message_id, text_hash, created_at)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING session_id",
            (session_id, body.client_message_id, digest, now),
        ).fetchone()
        if inserted is not None:
            return None
        prior = fetch_one(
            conn,
            "SELECT text_hash, created_at, response FROM api_requests"
            " WHERE session_id = %s AND client_message_id = %s FOR UPDATE",
            (session_id, body.client_message_id),
        )
        if prior is None:
            raise HTTPException(status_code=409, detail="request is being handled; retry shortly")
        if prior["text_hash"] != digest:
            raise HTTPException(status_code=409, detail="this message ID was used for a different message")
        if prior["response"] is not None:
            return MessageOut(**prior["response"], replayed=True)
        if now - prior["created_at"] < STALE_IN_FLIGHT:
            raise HTTPException(status_code=409, detail="still working on that message; retry shortly")
        conn.execute(  # an abandoned request (server died mid-turn): handle it again
            "UPDATE api_requests SET created_at = %s WHERE session_id = %s AND client_message_id = %s",
            (now, session_id, body.client_message_id),
        )
    return None
