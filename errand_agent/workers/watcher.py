"""Provider-cancellation watcher (PDF §3 proactive follow-up; DECISIONS §8; E24, E44).

Deterministic, no language model. Without any user message it:

1. ingests the provider's cancellation events idempotently (one canonical event per booking;
   every duplicate delivery is recorded against it and never processed twice);
2. checks the source of truth (`list_bookings`) before acting: an event for a booking that isn't
   actually cancelled by the provider, or is already in the past, is recorded and ignored;
3. invalidates any pending approval card that targets the cancelled booking;
4. searches for a replacement on the same day, within the constraints the user approved the
   original booking under and their confirmed saved preferences, excluding the provider who
   cancelled; ranks deterministically; holds the best option;
5. tells the user through the outbox, in a new follow-up conversation: the cancellation, the
   options and an approval card that needs fresh approval (nothing is ever rebooked
   automatically), or a no-match explanation. The follow-up conversation keeps the request, so
   the user can simply reply with another day or time.

A re-run after a crash reuses the follow-up conversation and its pending card, and the outbox key
per event prevents a second notification. A crash after a hold but before the card can leave one
extra hold, which expires on its own after 10 minutes; it never creates a booking.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from errand_agent import queue, trace
from errand_agent.claims import ClaimManager, verify_claim
from errand_agent.clock import Clock
from errand_agent.db import Database, fetch_all, fetch_one
from errand_agent.memory.facts import EarliestTimeValue, ProviderValue
from errand_agent.memory.store import MemoryStore
from errand_agent.queue import Job
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError
from errand_agent.workers.outbox import notify
from errand_agent.workflow import templates
from errand_agent.workflow.agent import request_constraints
from errand_agent.workflow.approval import ApprovalGate
from errand_agent.workflow.budget import Run, RunManager, reserve_run
from errand_agent.workflow.parsing import LOCAL_TZ, Budget, DateWindow, TimeWindow
from errand_agent.workflow.ranking import rank
from errand_agent.workflow.request import RequestFacts, Workspace
from errand_agent.workflow.snapshots import BookSnapshot

HOLD_ATTEMPTS = 2
WatchFault = Callable[[str], None]


class ProviderEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    booking_id: str
    user_id: str
    status: str
    followup_session_id: int | None


class CancellationWatcher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        tools: ToolClient,
        gate: ApprovalGate,
        *,
        claims: ClaimManager,
        runs: RunManager,
        memory: MemoryStore | None = None,
        fault: WatchFault | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._tools = tools
        self._gate = gate
        self._claims = claims
        self._runs = runs
        self._memory = memory
        self.fault = fault

    # ================================================================== ingestion
    def ingest(self, *, limit: int = 100) -> int:
        """Read new feed events; returns how many new cancellations were queued for handling."""
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT e.id, e.kind, e.booking_id, e.user_id FROM mock_provider_events e WHERE NOT EXISTS ("
                " SELECT 1 FROM provider_event_sources s WHERE s.source_event_id = e.id)"
                " ORDER BY e.emitted_at, e.id LIMIT %s",
                (limit,),
            )
        queued = 0
        for r in rows:
            now = self._clock.now()
            with self._db.txn() as conn:
                created = conn.execute(
                    "INSERT INTO provider_events (id, source_event_id, kind, booking_id, user_id, received_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (uuid4(), r["id"], r["kind"], r["booking_id"], r["user_id"], now),
                ).fetchone()
                if created is not None:
                    event_id = created["id"]
                    queue.enqueue(conn, kind="provider_cancellation", event_id=event_id, due_at=now, now=now)
                    queued += 1
                else:
                    existing = fetch_one(
                        conn,
                        "SELECT id FROM provider_events WHERE booking_id = %s AND kind = %s",
                        (r["booking_id"], r["kind"]),
                    )
                    if existing is None:
                        raise RuntimeError(f"provider event {r['id']} conflicted but no canonical event exists")
                    event_id = existing["id"]
                conn.execute(
                    "INSERT INTO provider_event_sources (source_event_id, event_id, received_at) VALUES (%s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (r["id"], event_id, now),
                )
                trace.record(
                    conn,
                    "provider_event.ingested",
                    {
                        "source_event_id": r["id"],
                        "event_id": str(event_id),
                        "duplicate": created is None,
                        "booking_id": r["booking_id"],
                    },
                    at=now,
                )
        return queued

    # ================================================================== handling (a job)
    def handle(self, job: Job, holder: str) -> str:
        if job.event_id is None:
            raise ValueError(f"provider_cancellation job {job.id} has no event")
        event = self._event(job.event_id)
        if event.status != "received":
            return f"already {event.status}"
        session_id = self._followup_session(event)
        claim = self._claims.acquire(session_id, holder)
        run = self._runs.start(claim)
        try:
            return self._process(run, event)
        finally:
            self._runs.end(run, "job:provider_cancellation")
            self._claims.release(claim)

    def _process(self, run: Run, event: ProviderEvent) -> str:
        ctx = ToolContext(user_id=event.user_id, session_id=run.session_id, run_id=run.id)
        pending = self._pending_proposal(run.session_id)
        if pending is not None:  # a re-run after a crash: the card already exists
            return self._offer(run, event, pending, text=None)
        self._reserve_tool(run)
        booking = next(
            (
                b
                for b in self._tools.list_bookings(ctx, {"user_id": event.user_id}).bookings
                if b.booking_id == event.booking_id
            ),
            None,
        )
        if booking is None or booking.status != "cancelled_by_provider":
            status = booking.status if booking else "missing"
            return self._close(run, event, "ignored", {"reason": f"booking is {status}, not cancelled by the provider"})
        if booking.slot <= self._clock.now():
            return self._close(run, event, "ignored", {"reason": "the booking was already in the past"})
        self._invalidate_cards(run, event)

        constraints = self._constraints(event.booking_id)
        area = constraints.get("area")
        if not area:
            self._reserve_tool(run)
            area = self._tools.get_provider(ctx, {"provider_id": booking.provider_id}).area
        day = booking.slot.astimezone(LOCAL_TZ).date()
        window = DateWindow(start=day, end=day, source="the same day")
        times = (
            TimeWindow.model_validate(constraints["time"])
            if constraints.get("time")
            else self._saved_time(event.user_id, booking.service)
        )
        budget = Budget.model_validate(constraints["budget"]) if constraints.get("budget") else None
        favoured = self._saved_provider(event.user_id, booking.service, exclude=booking.provider_id)

        self._reserve_tool(run)
        found = self._tools.search_providers(
            ctx,
            {
                "service": booking.service,
                "area": area,
                "date_range": {"start": day.isoformat(), "end": day.isoformat()},
            },
        ).providers
        candidates = [p for p in found if p.provider_id != booking.provider_id]
        ranking = rank(candidates, window=window, times=times, budget=budget, favoured_provider_id=favoured)
        req = RequestFacts(intent="book", service=booking.service, area=area, date=window, time=times, budget=budget)
        self._save(run, req, Workspace(options=ranking.options, selected_option=1 if ranking.options else None))
        if self.fault:
            self.fault("after_search")

        intro = templates.provider_cancelled(booking)
        day_text = templates.window_text(day, day)
        for option in ranking.options[:HOLD_ATTEMPTS]:
            self._reserve_tool(run)
            try:
                hold = self._tools.hold_slot(ctx, {"provider_id": option.provider_id, "slot": option.slot.isoformat()})
            except ToolRejectedError:
                continue  # taken meanwhile: try the next option
            if budget is not None and budget.max_inr is not None and hold.quoted_price_inr > budget.max_inr:
                continue
            if self.fault:
                self.fault("after_hold")
            snapshot = BookSnapshot(
                provider_id=option.provider_id,
                provider_name=option.provider_name,
                service=booking.service,
                slot=hold.slot,
                price_inr=hold.quoted_price_inr,
                hold_id=hold.hold_id,
                hold_expires_at=hold.expires_at,
            )
            proposal = self._gate.propose(run.session_id, snapshot, run_id=run.id, constraints=request_constraints(req))
            heading = templates.replacement_heading(booking.service, area, day_text, _constraint_notes(req))
            text = "\n".join([intro, templates.comparison(ranking.options, heading), templates.REPLACEMENT_HELD])
            return self._offer(run, event, proposal.id, text=text)

        reasons = templates.no_match(ranking.excluded_over_budget, ranking.excluded_no_slot, 0)
        text = "\n".join([intro, templates.replacement_none(booking.service, area, day_text, reasons)])
        with self._db.session_txn(run.session_id) as conn:
            notify(
                conn,
                user_id=event.user_id,
                session_id=run.session_id,
                event_key=f"provider_cancelled:{event.id}",
                kind="provider_cancelled",
                text=text,
                now=self._clock.now(),
            )
        return self._close(run, event, "handled", {"proposal_id": None, "options": 0})

    # ================================================================== steps
    def _offer(self, run: Run, event: ProviderEvent, proposal_id: UUID, *, text: str | None) -> str:
        with self._db.session_txn(run.session_id) as conn:
            if text is None:  # re-run: rebuild the notice from the stored request
                text = (
                    f"The provider cancelled your booking {event.booking_id}. Here is the replacement I held; "
                    "nothing is booked until you approve it."
                )
            notify(
                conn,
                user_id=event.user_id,
                session_id=run.session_id,
                event_key=f"provider_cancelled:{event.id}",
                kind="provider_cancelled",
                text=text,
                now=self._clock.now(),
                proposal_id=proposal_id,
            )
        return self._close(run, event, "handled", {"proposal_id": str(proposal_id)})

    def _close(self, run: Run, event: ProviderEvent, status: str, outcome: dict[str, Any]) -> str:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            conn.execute(
                "UPDATE provider_events SET status = %s, outcome = %s WHERE id = %s AND status = 'received'",
                (status, Jsonb(outcome), event.id),
            )
            trace.record(
                conn,
                "provider_event.handled",
                {"event_id": str(event.id), "booking_id": event.booking_id, "status": status, **outcome},
                at=now,
                run_id=run.id,
                session_id=run.session_id,
            )
        return f"{status}: {outcome}"

    def _invalidate_cards(self, run: Run, event: ProviderEvent) -> None:
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT DISTINCT p.session_id FROM proposals p JOIN sessions s ON s.id = p.session_id"
                " WHERE s.user_id = %s AND p.status = 'pending' AND p.snapshot->>'booking_id' = %s",
                (event.user_id, event.booking_id),
            )
        for r in rows:
            self._gate.invalidate_pending(int(r["session_id"]), "the provider cancelled this booking")

    def _constraints(self, booking_id: str) -> dict[str, Any]:
        """What the user approved the original booking under (empty for bookings made elsewhere)."""
        with self._db.txn() as conn:
            row = fetch_one(
                conn,
                "SELECT constraints FROM operations WHERE action = 'book' AND status = 'succeeded'"
                " AND outcome->>'booking_id' = %s ORDER BY updated_at DESC LIMIT 1",
                (booking_id,),
            )
        return dict(row["constraints"]) if row and row["constraints"] else {}

    def _saved_time(self, user_id: str, service: str) -> TimeWindow | None:
        if self._memory is None:
            return None
        fact = self._memory.current(user_id, "earliest_time", service) or self._memory.current(  # type: ignore[arg-type]
            user_id, "earliest_time", "global"
        )
        if fact is None or not isinstance(fact.value, EarliestTimeValue):
            return None
        clock = fact.value.earliest.strftime("%I:%M %p").lstrip("0")
        return TimeWindow(earliest=fact.value.earliest, source=f"not before {clock} (saved)")

    def _saved_provider(self, user_id: str, service: str, *, exclude: str) -> str | None:
        if self._memory is None:
            return None
        fact = self._memory.current(user_id, "preferred_provider", service)  # type: ignore[arg-type]
        if fact is None or not isinstance(fact.value, ProviderValue) or fact.value.provider_id == exclude:
            return None
        return fact.value.provider_id

    # ================================================================== helpers
    def _event(self, event_id: UUID) -> ProviderEvent:
        with self._db.txn() as conn:
            row = fetch_one(
                conn,
                "SELECT id, booking_id, user_id, status, followup_session_id FROM provider_events WHERE id = %s",
                (event_id,),
            )
        if row is None:
            raise ValueError(f"provider event {event_id} not found")
        return ProviderEvent.model_validate(dict(row))

    def _followup_session(self, event: ProviderEvent) -> int:
        """The conversation the follow-up happens in; created once per event."""
        now = self._clock.now()
        with self._db.txn() as conn:
            row = fetch_one(
                conn, "SELECT followup_session_id FROM provider_events WHERE id = %s FOR UPDATE", (event.id,)
            )
            if row is not None and row["followup_session_id"] is not None:
                return int(row["followup_session_id"])
            created = fetch_one(
                conn,
                "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s) RETURNING id",
                (event.user_id, now, now),
            )
            if created is None:
                raise RuntimeError("could not create a follow-up session")
            conn.execute("UPDATE provider_events SET followup_session_id = %s WHERE id = %s", (created["id"], event.id))
            return int(created["id"])

    def _pending_proposal(self, session_id: int) -> UUID | None:
        with self._db.txn() as conn:
            row = fetch_one(
                conn, "SELECT id FROM proposals WHERE session_id = %s AND status = 'pending'", (session_id,)
            )
        return None if row is None else UUID(str(row["id"]))

    def _save(self, run: Run, req: RequestFacts, ws: Workspace) -> None:
        now = self._clock.now()
        with self._db.session_txn(run.session_id) as conn:
            verify_claim(conn, run.session_id, run.claim.holder, run.claim.generation, now)
            conn.execute(
                "UPDATE sessions SET request = %s, workspace = %s, revision = revision + 1, updated_at = %s"
                " WHERE id = %s",
                (Jsonb(req.model_dump(mode="json")), Jsonb(ws.model_dump(mode="json")), now, run.session_id),
            )

    def _reserve_tool(self, run: Run) -> None:
        with self._db.session_txn(run.session_id) as conn:
            reserve_run(conn, run, "tool_invocations", self._clock.now())


def _constraint_notes(req: RequestFacts) -> list[str]:
    notes: list[str] = []
    if req.time is not None:
        notes.append(req.time.source)
    if req.budget is not None and req.budget.max_inr is not None:
        notes.append(f"up to {templates.money(req.budget.max_inr)}")
    return notes
