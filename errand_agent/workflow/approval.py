"""Approval gate: code-enforced, revision-bound confirmation of consequential actions.

Rules (DECISIONS §§5–6):
- A proposal is an immutable snapshot; any material change creates a new revision and supersedes
  the old one. One pending proposal per session (also a DB constraint).
- A confirmation must cite a display receipt issued when code actually rendered the card.
- Both `yes` and `confirm <token>` bind only to that receipt's revision, and only while it is
  still the pending, unexpired, latest revision. A stale yes never transfers to a newer card.
- Approval consumption, the operation and its stable idempotency key commit in one transaction,
  under the session advisory lock. Repeating a confirmation returns the existing operation.
- This module parses only literal user text; nothing a model or a tool emits can reach it.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent import trace
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_one
from errand_agent.errors import ErrandError
from errand_agent.workflow import templates
from errand_agent.workflow.snapshots import SNAPSHOT, BookSnapshot, CancelSnapshot, RescheduleSnapshot, snapshot_hash
from errand_agent.workflow.states import PROPOSABLE, current_state, transition

AnySnapshot = BookSnapshot | CancelSnapshot | RescheduleSnapshot
UNRESOLVED_OPERATION_STATES = ("approved", "executing", "verifying", "retry_wait", "pending_verification")
_TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # noqa: S105 - alphabet, not a secret; no 0/O, 1/I/L
TOKEN_LENGTH = 6
_YES = re.compile(r"^\s*yes\s*[.!]?\s*$", re.IGNORECASE)
_CONFIRM = re.compile(rf"^\s*confirm\s+([A-Za-z0-9]{{{TOKEN_LENGTH}}})\s*[.!]?\s*$", re.IGNORECASE)


class ProposalBlockedError(ErrandError):
    user_message = "I can't prepare a new action until the current one is resolved."


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    session_id: int
    revision: int
    snapshot: AnySnapshot
    token: str
    status: Literal["pending", "approved", "superseded", "invalidated"]


class DisplayReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    proposal_id: UUID
    revision: int
    text: str


RejectReason = Literal[
    "no_card_displayed",
    "unknown_receipt",
    "not_owner",
    "token_mismatch",
    "stale_revision",
    "invalidated",
    "hold_expired",
]


class ConfirmationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["approved", "duplicate", "rejected", "not_confirmation"]
    reason: RejectReason | None = None
    operation_id: UUID | None = None
    message: str = ""


def parse_confirmation(text: str) -> tuple[Literal["yes", "token"], str | None] | None:
    """Exactly `yes` or `confirm <TOKEN>` (case/whitespace/one trailing ./! tolerated). Else None."""
    if _YES.match(text):
        return "yes", None
    match = _CONFIRM.match(text)
    if match:
        return "token", match.group(1).upper()
    return None


class ApprovalGate:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ------------------------------------------------------------------ proposals
    def propose(self, session_id: int, snapshot: AnySnapshot, *, run_id: UUID | None = None) -> Proposal:
        """Create (or return the identical) pending proposal; supersede a materially different one."""
        now = self._clock.now()
        digest = snapshot_hash(snapshot)
        with self._db.session_txn(session_id) as conn:
            state, _ = current_state(conn, session_id)
            if state not in PROPOSABLE:
                raise ProposalBlockedError(f"session {session_id} is {state}")
            unresolved = fetch_one(
                conn,
                "SELECT id FROM operations WHERE session_id = %s AND status = ANY(%s) LIMIT 1",
                (session_id, list(UNRESOLVED_OPERATION_STATES)),
            )
            if unresolved is not None:
                raise ProposalBlockedError(f"operation {unresolved['id']} is unresolved")
            pending = fetch_one(
                conn, "SELECT * FROM proposals WHERE session_id = %s AND status = 'pending'", (session_id,)
            )
            if pending is not None and pending["snapshot_hash"] == digest:
                return _proposal(pending)
            if pending is not None:
                conn.execute(
                    "UPDATE proposals SET status = 'superseded', status_reason = 'material change' WHERE id = %s",
                    (pending["id"],),
                )
            row = fetch_one(
                conn, "SELECT coalesce(max(revision), 0) + 1 AS r FROM proposals WHERE session_id = %s", (session_id,)
            )
            revision = int(row["r"]) if row else 1
            proposal_id = uuid4()
            token = _insert_proposal(
                conn,
                proposal_id=proposal_id,
                session_id=session_id,
                revision=revision,
                snapshot=snapshot,
                digest=digest,
                supersedes=pending,
                now=now,
            )
            transition(conn, session_id, "awaiting_approval", now=now, reason=f"proposal r{revision}", run_id=run_id)
            trace.record(
                conn,
                "proposal.created",
                {
                    "proposal_id": str(proposal_id),
                    "revision": revision,
                    "action": snapshot.action,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "supersedes": str(pending["id"]) if pending else None,
                },
                at=now,
                run_id=run_id,
                session_id=session_id,
            )
        return Proposal(
            id=proposal_id, session_id=session_id, revision=revision, snapshot=snapshot, token=token, status="pending"
        )

    def display(self, session_id: int, proposal_id: UUID) -> DisplayReceipt:
        """Render the approval card from stored facts and issue the receipt a confirmation must cite."""
        now = self._clock.now()
        with self._db.session_txn(session_id) as conn:
            row = fetch_one(
                conn, "SELECT * FROM proposals WHERE id = %s AND session_id = %s", (proposal_id, session_id)
            )
            if row is None or row["status"] != "pending":
                raise ProposalBlockedError(f"proposal {proposal_id} is not the pending proposal")
            proposal = _proposal(row)
            text = templates.approval_card(proposal.snapshot, proposal.token, proposal.revision)
            receipt_id = uuid4()
            conn.execute(
                "INSERT INTO display_receipts (id, session_id, proposal_id, displayed_at) VALUES (%s, %s, %s, %s)",
                (receipt_id, session_id, proposal_id, now),
            )
            trace.record(
                conn,
                "approval.card_displayed",
                {"proposal_id": str(proposal_id), "revision": proposal.revision, "receipt_id": str(receipt_id)},
                at=now,
                session_id=session_id,
            )
        return DisplayReceipt(id=receipt_id, proposal_id=proposal_id, revision=proposal.revision, text=text)

    def invalidate_pending(self, session_id: int, reason: str) -> None:
        """A correction or external change makes the pending card unusable (no transfer)."""
        now = self._clock.now()
        with self._db.session_txn(session_id) as conn:
            updated = conn.execute(
                "UPDATE proposals SET status = 'invalidated', status_reason = %s"
                " WHERE session_id = %s AND status = 'pending' RETURNING id",
                (reason[:200], session_id),
            ).fetchone()
            if updated is not None:
                transition(conn, session_id, "collecting", now=now, reason=f"invalidated: {reason}")
                trace.record(
                    conn,
                    "proposal.invalidated",
                    {"proposal_id": str(updated["id"]), "reason": reason},
                    at=now,
                    session_id=session_id,
                )

    # ------------------------------------------------------------------ confirmation
    def confirm(self, session_id: int, user_id: str, text: str, receipt_id: UUID | None) -> ConfirmationResult:
        parsed = parse_confirmation(text)
        if parsed is None:
            return ConfirmationResult(outcome="not_confirmation")
        form, token = parsed
        now = self._clock.now()
        with self._db.session_txn(session_id) as conn:
            result = self._confirm_locked(
                conn, session_id=session_id, user_id=user_id, form=form, token=token, receipt_id=receipt_id, now=now
            )
            trace.record(
                conn,
                "approval.decision",
                {
                    "form": form,
                    "receipt_id": str(receipt_id) if receipt_id else None,
                    "outcome": result.outcome,
                    "reason": result.reason,
                    "operation_id": str(result.operation_id) if result.operation_id else None,
                },
                at=now,
                session_id=session_id,
                operation_id=result.operation_id,
            )
        return result

    def _confirm_locked(  # noqa: PLR0911 - an early-exit decision table reads clearest
        self,
        conn: Conn,
        *,
        session_id: int,
        user_id: str,
        form: Literal["yes", "token"],
        token: str | None,
        receipt_id: UUID | None,
        now: datetime,
    ) -> ConfirmationResult:
        session = fetch_one(conn, "SELECT user_id FROM sessions WHERE id = %s", (session_id,))
        if session is None or session["user_id"] != user_id:
            return _reject("not_owner", templates.NOT_OWNER)
        if receipt_id is None:
            return _reject("no_card_displayed", templates.NO_CARD)
        row = fetch_one(
            conn,
            "SELECT p.* FROM display_receipts r JOIN proposals p ON p.id = r.proposal_id"
            " WHERE r.id = %s AND r.session_id = %s AND p.session_id = %s",
            (receipt_id, session_id, session_id),
        )
        if row is None:
            return _reject("unknown_receipt", templates.NO_CARD)
        proposal = _proposal(row)
        if form == "token" and token != proposal.token:
            return _reject("token_mismatch", templates.CODE_MISMATCH)

        if proposal.status == "approved":
            op = fetch_one(conn, "SELECT id FROM operations WHERE proposal_id = %s", (proposal.id,))
            if op is None:  # approvals and operations are created together; this would be corruption
                raise ErrandError(f"approved proposal {proposal.id} has no operation")
            return ConfirmationResult(outcome="duplicate", operation_id=op["id"])
        if proposal.status == "superseded":
            latest = fetch_one(
                conn, "SELECT * FROM proposals WHERE session_id = %s ORDER BY revision DESC LIMIT 1", (session_id,)
            )
            newer = _proposal(latest).snapshot if latest else proposal.snapshot
            return _reject("stale_revision", templates.material_change(proposal.snapshot, newer))
        if proposal.status == "invalidated":
            if row["status_reason"] == "hold_expired" and isinstance(proposal.snapshot, BookSnapshot):
                return _reject("hold_expired", templates.hold_expired(proposal.snapshot.slot))
            return _reject("invalidated", templates.INVALIDATED)

        # Pending: last-moment freshness checks before consuming approval.
        if isinstance(proposal.snapshot, BookSnapshot) and now >= proposal.snapshot.hold_expires_at:
            conn.execute(
                "UPDATE proposals SET status = 'invalidated', status_reason = 'hold_expired' WHERE id = %s",
                (proposal.id,),
            )
            transition(conn, session_id, "comparing", now=now, reason="hold expired before approval")
            return _reject("hold_expired", templates.hold_expired(proposal.snapshot.slot))

        approval_id, operation_id = uuid4(), uuid4()
        key = f"op-{uuid4().hex}"
        conn.execute(
            "INSERT INTO approvals (id, proposal_id, session_id, receipt_id, form, approved_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (approval_id, proposal.id, session_id, receipt_id, form, now),
        )
        conn.execute("UPDATE proposals SET status = 'approved' WHERE id = %s", (proposal.id,))
        conn.execute(
            "INSERT INTO operations (id, session_id, user_id, proposal_id, approval_id, action, snapshot,"
            " idempotency_key, status, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s, %s)",
            (
                operation_id,
                session_id,
                user_id,
                proposal.id,
                approval_id,
                proposal.snapshot.action,
                Jsonb(proposal.snapshot.model_dump(mode="json")),
                key,
                now,
                now,
            ),
        )
        transition(conn, session_id, "executing", now=now, reason="approval consumed", operation_id=operation_id)
        trace.record(
            conn,
            "operation.created",
            {
                "operation_id": str(operation_id),
                "proposal_id": str(proposal.id),
                "revision": proposal.revision,
                "action": proposal.snapshot.action,
                "idempotency_key": key,
            },
            at=now,
            session_id=session_id,
            operation_id=operation_id,
        )
        return ConfirmationResult(outcome="approved", operation_id=operation_id)


def _reject(reason: RejectReason, message: str) -> ConfirmationResult:
    return ConfirmationResult(outcome="rejected", reason=reason, message=message)


def _proposal(row: dict[str, Any]) -> Proposal:
    return Proposal(
        id=row["id"],
        session_id=row["session_id"],
        revision=row["revision"],
        snapshot=SNAPSHOT.validate_python(row["snapshot"]),
        token=row["token"],
        status=row["status"],
    )


def _insert_proposal(
    conn: Conn,
    *,
    proposal_id: UUID,
    session_id: int,
    revision: int,
    snapshot: AnySnapshot,
    digest: str,
    supersedes: dict[str, Any] | None,
    now: datetime,
) -> str:
    """Insert with a fresh random token; retry on the (unlikely) global token collision."""
    for _ in range(5):
        token = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
        clash = fetch_one(conn, "SELECT 1 AS x FROM proposals WHERE token = %s", (token,))
        if clash is not None:
            continue
        conn.execute(
            "INSERT INTO proposals (id, session_id, revision, action, snapshot, snapshot_hash, token, status,"
            " supersedes, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)",
            (
                proposal_id,
                session_id,
                revision,
                snapshot.action,
                Jsonb(snapshot.model_dump(mode="json")),
                digest,
                token,
                supersedes["id"] if supersedes else None,
                now,
            ),
        )
        return token
    raise ErrandError("could not allocate a unique confirmation token")
