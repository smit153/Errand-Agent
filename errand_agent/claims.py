"""Durable per-session claims with generation fencing.

Advisory locks only serialize work *inside* transactions. A claim (holder, generation, lease)
prevents two workers from advancing one conversation while either is doing model/tool work
outside a transaction. Every acquisition bumps the generation, so a delayed worker holding an
older generation can neither commit workflow results nor commit a backend side effect.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from errand_agent import trace
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.errors import SessionBusyError, StaleClaimError


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: int
    holder: str
    generation: int
    lease_expires_at: datetime


class ClaimManager:
    def __init__(self, db: Database, clock: Clock, *, lease_seconds: int = 60) -> None:
        self._db = db
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)

    def acquire(self, session_id: int, holder: str) -> Claim:
        """Claim a session. Fails with SessionBusyError while another holder's lease is live.

        Taking over an expired lease fences the previous holder (generation + 1).
        """
        with self._db.session_txn(session_id) as conn:
            now = self._clock.now()
            row = fetch_one(
                conn,
                "SELECT generation, holder, lease_expires_at FROM session_claims WHERE session_id = %s",
                (session_id,),
            )
            expires = now + self._lease
            if row is None:
                conn.execute(
                    "INSERT INTO session_claims (session_id, generation, holder, lease_expires_at, claimed_at)"
                    " VALUES (%s, 1, %s, %s, %s)",
                    (session_id, holder, expires, now),
                )
                generation = 1
                previous: str | None = None
            else:
                live = row["holder"] is not None and row["lease_expires_at"] > now
                if live and row["holder"] != holder:
                    raise SessionBusyError(f"session {session_id} claimed by {row['holder']}")
                generation = int(row["generation"]) + 1
                previous = row["holder"]
                conn.execute(
                    "UPDATE session_claims SET generation = %s, holder = %s, lease_expires_at = %s,"
                    " claimed_at = %s WHERE session_id = %s",
                    (generation, holder, expires, now, session_id),
                )
            trace.record(
                conn,
                "claim.acquired",
                {"holder": holder, "generation": generation, "fenced_holder": previous},
                at=now,
                session_id=session_id,
            )
        return Claim(session_id=session_id, holder=holder, generation=generation, lease_expires_at=expires)

    def verify(self, conn: Conn, claim: Claim) -> None:
        """Inside a session transaction: raise StaleClaimError unless `claim` is current and live."""
        verify_claim(conn, claim.session_id, claim.holder, claim.generation, self._clock.now())

    def renew(self, claim: Claim) -> Claim:
        with self._db.session_txn(claim.session_id) as conn:
            self.verify(conn, claim)
            expires = self._clock.now() + self._lease
            conn.execute(
                "UPDATE session_claims SET lease_expires_at = %s WHERE session_id = %s",
                (expires, claim.session_id),
            )
        return claim.model_copy(update={"lease_expires_at": expires})

    def release(self, claim: Claim) -> None:
        """Release if still ours; releasing a fenced claim is a no-op."""
        with self._db.session_txn(claim.session_id) as conn:
            conn.execute(
                "UPDATE session_claims SET holder = NULL, lease_expires_at = NULL"
                " WHERE session_id = %s AND generation = %s AND holder = %s",
                (claim.session_id, claim.generation, claim.holder),
            )


def verify_claim(conn: Conn, session_id: int, holder: str, generation: int, now: datetime) -> None:
    """Shared fencing check, also used by the mock backend inside its mutation transaction.

    The caller must already hold the session advisory lock in this transaction.
    """
    row = fetch_one(
        conn,
        "SELECT generation, holder, lease_expires_at FROM session_claims WHERE session_id = %s",
        (session_id,),
    )
    if row is None:
        raise StaleClaimError(f"session {session_id} has no claim")
    if int(row["generation"]) != generation or row["holder"] != holder:
        raise StaleClaimError(
            f"claim fenced: have gen {generation}/{holder}, current gen {row['generation']}/{row['holder']}"
        )
    if row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
        raise StaleClaimError(f"claim lease expired for session {session_id} gen {generation}")
