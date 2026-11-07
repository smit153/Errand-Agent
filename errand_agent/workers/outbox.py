"""User notifications: outbox with at-least-once delivery and stable IDs (DECISIONS §9; E44).

A notification is written in the same transaction as the state change it reports, under a
stable `event_key`, so a retried or duplicated event can't notify twice. A client fetches
undelivered notifications (the same IDs again until it acknowledges them), so a crash before or
after showing them loses nothing; the client de-duplicates by ID. Delivery is not exactly-once and
is never claimed to be.

A notification can carry an approval card. The card is rendered from the stored proposal when it
is fetched, and that fetch records the display receipt, so a confirmation can only cite a card
the user was actually shown. A card that is no longer pending is delivered as "no longer
available", never as an approvable card.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_all
from errand_agent.workflow import templates
from errand_agent.workflow.approval import ApprovalGate, ProposalBlockedError

NotificationKind = Literal["operation_outcome", "operation_pending", "provider_cancelled"]


class Notification(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    session_id: int | None
    text: str
    receipt_id: UUID | None = None
    created_at: datetime


def notify(
    conn: Conn,
    *,
    user_id: str,
    session_id: int | None,
    event_key: str,
    kind: NotificationKind,
    text: str,
    now: datetime,
    proposal_id: UUID | None = None,
) -> bool:
    """Queue a notification once per `event_key`. Returns False if it was already queued."""
    payload: dict[str, Any] = {"text": text, "proposal_id": str(proposal_id) if proposal_id else None}
    row = conn.execute(
        "INSERT INTO outbox (id, user_id, session_id, event_key, kind, payload, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (event_key) DO NOTHING RETURNING id",
        (uuid4(), user_id, session_id, event_key[:200], kind, Jsonb(payload), now),
    ).fetchone()
    return row is not None


class Outbox:
    def __init__(self, db: Database, clock: Clock, gate: ApprovalGate) -> None:
        self._db = db
        self._clock = clock
        self._gate = gate

    def fetch(self, user_id: str, *, limit: int = 20) -> list[Notification]:
        """Undelivered notifications, oldest first. Repeats until acknowledged (at-least-once)."""
        now = self._clock.now()
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "UPDATE outbox SET fetch_count = fetch_count + 1, last_fetched_at = %s WHERE id IN ("
                " SELECT id FROM outbox WHERE user_id = %s AND delivered_at IS NULL"
                " ORDER BY created_at, id LIMIT %s) RETURNING id, kind, session_id, payload, created_at",
                (now, user_id, limit),
            )
        out: list[Notification] = []
        for r in sorted(rows, key=lambda r: (r["created_at"], str(r["id"]))):
            text = str(r["payload"].get("text", ""))
            receipt_id: UUID | None = None
            proposal = r["payload"].get("proposal_id")
            if proposal and r["session_id"] is not None:
                try:
                    receipt = self._gate.display(int(r["session_id"]), UUID(proposal))
                    text, receipt_id = f"{text}\n{receipt.text}", receipt.id
                except ProposalBlockedError:
                    text = f"{text}\n{templates.OFFER_GONE}"
            out.append(
                Notification(
                    id=r["id"],
                    kind=r["kind"],
                    session_id=r["session_id"],
                    text=text,
                    receipt_id=receipt_id,
                    created_at=r["created_at"],
                )
            )
        return out

    def ack(self, user_id: str, ids: list[UUID]) -> int:
        """Mark notifications delivered (only the caller's own). Returns how many changed."""
        with self._db.txn() as conn:
            return conn.execute(
                "UPDATE outbox SET delivered_at = %s WHERE user_id = %s AND id = ANY(%s) AND delivered_at IS NULL",
                (self._clock.now(), user_id, ids),
            ).rowcount
