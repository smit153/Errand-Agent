"""Postgres-backed mock provider backend implementing the eight fixed tool contracts.

Scenario controls (forced outcomes, quote drift, conflicts) are test/ops controls, not tools.
All time comes from the injected clock. Read-only tools take no locks beyond MVCC; mutating tools
take the session advisory lock (for fencing), then the mock-global lock, then rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from errand_agent.claims import verify_claim
from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_all, fetch_one
from errand_agent.errors import StaleClaimError
from errand_agent.mocks.catalog import (
    DEMO_USER,
    LOCAL_TZ,
    ProviderSeed,
    build_catalog,
    build_history,
    local_slot,
)
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError, ToolTimeoutError, ToolTransientError
from errand_agent.tools.models import (
    BookingRecord,
    BookInput,
    BookOutput,
    GetBookingStatusInput,
    GetBookingStatusOutput,
    GetProviderInput,
    GetProviderOutput,
    HoldSlotInput,
    HoldSlotOutput,
    ListBookingsInput,
    ListBookingsOutput,
    ProviderSummary,
    Review,
    SearchProvidersInput,
    SearchProvidersOutput,
    utc,
)

MAX_SLOTS_PER_PROVIDER = 6
HOLD_TTL = timedelta(minutes=10)
MOCK_LOCK_KEY = -0x4D4F434B  # "MOCK"; negative so it never collides with bigserial session ids

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_lost"]
BOOK_OUTCOMES: tuple[BookOutcome, ...] = ("success", "slot_taken", "timeout_booked", "timeout_lost")


def uniform(seed: int, stream: str, counter: int) -> float:
    """Deterministic U[0,1) for draw number `counter` of `stream` (counter persisted in mock_rng)."""
    digest = hashlib.sha256(f"{seed}:{stream}:{counter}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def book_outcome(u: float) -> BookOutcome:
    """PDF §2: success 70%, slot_taken 15%, timeout 15% of which half actually booked."""
    if u < 0.70:
        return "success"
    if u < 0.85:
        return "slot_taken"
    if u < 0.925:
        return "timeout_booked"
    return "timeout_lost"


class ScenarioConfig(BaseModel):
    """Configurable branches for reproducible scenarios. Defaults reproduce the PDF behaviour."""

    model_config = ConfigDict(extra="forbid")

    book_outcomes: list[BookOutcome] = Field(default_factory=list)  # forced FIFO, then seeded draws
    quote_drift_inr: dict[str, int] = Field(default_factory=dict)  # provider_id -> delta on hold
    reschedule_conflict_slots: list[AwareDatetime] = Field(default_factory=list)
    cancel_extra_503: int = Field(default=0, ge=0)  # transient faults beyond the required first
    status_failures: int = Field(default=0, ge=0)  # next N get_booking_status calls raise 503


class MockBackend:
    def __init__(self, db: Database, clock: Clock, *, seed: int = 42) -> None:
        self._db = db
        self._clock = clock
        self._seed = seed

    # ------------------------------------------------------------------ setup / controls
    def seed(self, anchor: datetime) -> None:
        """Insert catalog, reviews, demo user and booking history. Idempotent."""
        providers, reviews = build_catalog(self._seed)
        history = build_history(providers, anchor)
        now = self._clock.now()
        with self._db.txn() as conn:
            conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (DEMO_USER,))
            for p in providers:
                conn.execute(
                    "INSERT INTO mock_providers (id, service, area, name, base_price_inr, rating, attributes,"
                    " schedule, description) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        p.id,
                        p.service,
                        p.area,
                        p.name,
                        p.base_price_inr,
                        p.rating,
                        Jsonb(p.attributes),
                        Jsonb({"weekdays": p.schedule_weekdays, "hours": p.schedule_hours}),
                        p.description,
                    ),
                )
            for r in reviews:
                conn.execute(
                    "INSERT INTO mock_reviews (id, provider_id, author, rating, body) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (r.id, r.provider_id, r.author, r.rating, r.body),
                )
            for b in history:
                conn.execute(
                    "INSERT INTO mock_bookings (id, user_id, provider_id, service, slot, price_inr, status,"
                    " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (b.id, b.user_id, b.provider_id, b.service, b.slot, b.price_inr, b.status, now, now),
                )
            for stream in ("book", "ids"):
                conn.execute(
                    "INSERT INTO mock_rng (stream, seed, counter) VALUES (%s, %s, 0) ON CONFLICT DO NOTHING",
                    (stream, self._seed),
                )
            conn.execute(
                "INSERT INTO mock_config (key, value) VALUES ('scenario', %s) ON CONFLICT DO NOTHING",
                (Jsonb(ScenarioConfig().model_dump(mode="json")),),
            )

    def configure(self, scenario: ScenarioConfig) -> None:
        with self._db.txn() as conn:
            conn.execute(
                "INSERT INTO mock_config (key, value) VALUES ('scenario', %s)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (Jsonb(scenario.model_dump(mode="json")),),
            )

    def scenario(self) -> ScenarioConfig:
        with self._db.txn() as conn:
            return _load_scenario(conn)

    # ------------------------------------------------------------------ read-only tools
    def search_providers(self, inp: SearchProvidersInput, ctx: ToolContext) -> SearchProvidersOutput:
        now = self._clock.now()
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT * FROM mock_providers WHERE service = %s AND area = %s ORDER BY id",
                (inp.service, inp.area),
            )
            if not rows:
                raise ToolRejectedError("search_providers", "no_providers", f"{inp.service}/{inp.area}")
            ids = [r["id"] for r in rows]
            busy = _busy_slots(conn, ids, now)
        summaries: list[ProviderSummary] = []
        for r in rows:
            slots = [
                s
                for s in _schedule_slots(r["schedule"], inp.date_range.start, inp.date_range.end)
                if s > now and (r["id"], s) not in busy
            ][:MAX_SLOTS_PER_PROVIDER]
            summaries.append(
                ProviderSummary(
                    provider_id=r["id"],
                    name=r["name"],
                    service=r["service"],
                    area=r["area"],
                    price_inr=r["base_price_inr"],
                    rating=float(r["rating"]),
                    available_slots=slots,
                    attributes=r["attributes"],
                )
            )
        return SearchProvidersOutput(providers=summaries)

    def get_provider(self, inp: GetProviderInput, ctx: ToolContext) -> GetProviderOutput:
        with self._db.txn() as conn:
            row = fetch_one(conn, "SELECT * FROM mock_providers WHERE id = %s", (inp.provider_id,))
            if row is None:
                raise ToolRejectedError("get_provider", "not_found", inp.provider_id)
            reviews = fetch_all(
                conn, "SELECT * FROM mock_reviews WHERE provider_id = %s ORDER BY id", (inp.provider_id,)
            )
        return GetProviderOutput(
            provider_id=row["id"],
            name=row["name"],
            service=row["service"],
            area=row["area"],
            price_inr=row["base_price_inr"],
            rating=float(row["rating"]),
            attributes=row["attributes"],
            description=row["description"],
            reviews=[
                Review(review_id=v["id"], author=v["author"], rating=v["rating"], text=v["body"]) for v in reviews
            ],
        )

    def list_bookings(self, inp: ListBookingsInput, ctx: ToolContext) -> ListBookingsOutput:
        if inp.user_id != ctx.user_id:
            raise ToolRejectedError("list_bookings", "forbidden", "user_id does not match the session user")
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT b.id, b.provider_id, p.name AS provider_name, b.service, b.slot, b.status"
                " FROM mock_bookings b JOIN mock_providers p ON p.id = b.provider_id"
                " WHERE b.user_id = %s ORDER BY b.slot, b.id",
                (inp.user_id,),
            )
        return ListBookingsOutput(
            bookings=[
                BookingRecord(
                    booking_id=r["id"],
                    provider_id=r["provider_id"],
                    provider_name=r["provider_name"],
                    service=r["service"],
                    slot=r["slot"],
                    status=r["status"],
                )
                for r in rows
            ]
        )

    # ------------------------------------------------------------------ hold_slot
    def hold_slot(self, inp: HoldSlotInput, ctx: ToolContext) -> HoldSlotOutput:
        now = self._clock.now()
        slot = utc(inp.slot)
        with self._db.session_txn(ctx.session_id) as conn:
            _lock_mock(conn)
            provider = fetch_one(conn, "SELECT * FROM mock_providers WHERE id = %s", (inp.provider_id,))
            if provider is None:
                raise ToolRejectedError("hold_slot", "not_found", inp.provider_id)
            if slot <= now or not is_schedule_slot(provider, slot):
                raise ToolRejectedError("hold_slot", "slot_unavailable", "not an open future slot")
            if (inp.provider_id, slot) in _busy_slots(conn, [inp.provider_id], now):
                raise ToolRejectedError("hold_slot", "slot_unavailable", "slot already held or booked")
            scenario = _load_scenario(conn)
            quote = int(provider["base_price_inr"]) + scenario.quote_drift_inr.get(inp.provider_id, 0)
            if quote <= 0:
                raise ToolRejectedError("hold_slot", "invalid_quote", "configured drift makes the quote non-positive")
            hold_id = _next_id(conn, "HLD")
            expires = now + HOLD_TTL
            conn.execute(
                "INSERT INTO mock_holds (id, user_id, provider_id, slot, quoted_price_inr, created_at, expires_at,"
                " status) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')",
                (hold_id, ctx.user_id, inp.provider_id, slot, quote, now, expires),
            )
        return HoldSlotOutput(
            hold_id=hold_id, provider_id=inp.provider_id, slot=slot, quoted_price_inr=quote, expires_at=expires
        )

    # ------------------------------------------------------------------ book
    def book(self, inp: BookInput, ctx: ToolContext) -> BookOutput:
        now = self._clock.now()
        result: BookOutput | None = None
        simulate_timeout = False
        with self._db.session_txn(ctx.session_id) as conn:
            _verify_fence(
                conn, ctx=ctx, tool="book", key=inp.idempotency_key, expected={"hold_id": inp.hold_id}, now=now
            )
            _lock_mock(conn)
            request_hash = _hash(inp.model_dump(mode="json"))
            replay = _replay(conn, "book", inp.idempotency_key, request_hash, now)
            if replay is not None:
                return BookOutput.model_validate(replay)
            hold = fetch_one(
                conn,
                "SELECT * FROM mock_holds WHERE id = %s AND user_id = %s FOR UPDATE",
                (inp.hold_id, ctx.user_id),
            )
            if hold is None:
                raise ToolRejectedError("book", "not_found", inp.hold_id)
            if hold["status"] != "active":
                raise ToolRejectedError("book", "hold_not_active", f"hold is {hold['status']}")
            if now >= hold["expires_at"]:
                conn.execute("UPDATE mock_holds SET status = 'released' WHERE id = %s", (inp.hold_id,))
                result = BookOutput(status="hold_expired", hold_id=inp.hold_id)
                _remember(
                    conn,
                    tool="book",
                    key=inp.idempotency_key,
                    request_hash=request_hash,
                    phase="terminal",
                    result=result,
                    now=now,
                )
                return result
            outcome = self._next_book_outcome(conn)
            if outcome in ("success", "timeout_booked"):
                result = self._commit_booking(conn, hold, ctx.user_id, now)
                _remember(
                    conn,
                    tool="book",
                    key=inp.idempotency_key,
                    request_hash=request_hash,
                    phase="committed",
                    result=result,
                    now=now,
                )
                simulate_timeout = outcome == "timeout_booked"
            elif outcome == "slot_taken":
                # Another customer won the slot: record their booking so the slot really is gone.
                conn.execute("UPDATE mock_holds SET status = 'released' WHERE id = %s", (inp.hold_id,))
                conn.execute(
                    "INSERT INTO mock_bookings (id, user_id, provider_id, service, slot, price_inr, status,"
                    " created_at, updated_at) SELECT %s, 'external', p.id, p.service, %s, %s, 'confirmed', %s, %s"
                    " FROM mock_providers p WHERE p.id = %s",
                    (_next_id(conn, "BKX"), hold["slot"], hold["quoted_price_inr"], now, now, hold["provider_id"]),
                )
                result = BookOutput(status="slot_taken", hold_id=inp.hold_id)
                _remember(
                    conn,
                    tool="book",
                    key=inp.idempotency_key,
                    request_hash=request_hash,
                    phase="terminal",
                    result=result,
                    now=now,
                )
            else:  # timeout_lost: nothing happened; the same key may be retried
                simulate_timeout = True
        if simulate_timeout or result is None:
            raise ToolTimeoutError("book", "simulated timeout after possibly succeeding")
        return result

    def _next_book_outcome(self, conn: Conn) -> BookOutcome:
        scenario = _load_scenario(conn)
        if scenario.book_outcomes:
            forced = scenario.book_outcomes.pop(0)
            _save_scenario(conn, scenario)
            return forced
        row = fetch_one(conn, "UPDATE mock_rng SET counter = counter + 1 WHERE stream = 'book' RETURNING seed, counter")
        if row is None:
            raise ToolRejectedError("book", "backend_not_seeded", "mock_rng stream 'book' missing")
        return book_outcome(uniform(int(row["seed"]), "book", int(row["counter"])))

    def _commit_booking(self, conn: Conn, hold: dict[str, Any], user_id: str, now: datetime) -> BookOutput:
        booking_id = _next_id(conn, "BK")
        try:
            with conn.transaction():  # savepoint: a unique violation must not abort the outer txn
                conn.execute(
                    "INSERT INTO mock_bookings (id, user_id, provider_id, service, slot, price_inr, status, hold_id,"
                    " created_at, updated_at) SELECT %s, %s, p.id, p.service, %s, %s, 'confirmed', %s, %s, %s"
                    " FROM mock_providers p WHERE p.id = %s",
                    (
                        booking_id,
                        user_id,
                        hold["slot"],
                        hold["quoted_price_inr"],
                        hold["id"],
                        now,
                        now,
                        hold["provider_id"],
                    ),
                )
        except pg_errors.UniqueViolation as exc:
            raise ToolRejectedError("book", "slot_unavailable", "slot was booked concurrently") from exc
        conn.execute("UPDATE mock_holds SET status = 'booked' WHERE id = %s", (hold["id"],))
        return BookOutput(
            status="confirmed",
            hold_id=hold["id"],
            booking_id=booking_id,
            provider_id=hold["provider_id"],
            slot=hold["slot"],
            price_inr=hold["quoted_price_inr"],
        )

    # ------------------------------------------------------------------ get_booking_status
    def get_booking_status(self, inp: GetBookingStatusInput, ctx: ToolContext) -> GetBookingStatusOutput:
        """Authoritative booking outcome, by hold id or by idempotency key."""
        now = self._clock.now()
        with self._db.txn() as conn:
            _lock_mock(conn)
            scenario = _load_scenario(conn)
            if scenario.status_failures > 0:
                scenario.status_failures -= 1
                _save_scenario(conn, scenario)
                fail = True
            else:
                fail = False
        if fail:  # committed the decrement first, so the failure budget is durable
            raise ToolTransientError("get_booking_status", 503, "configured status-service failure")
        with self._db.txn() as conn:
            if inp.idempotency_key is not None:
                return _status_by_key(conn, inp.idempotency_key, ctx.user_id)
            if inp.hold_id is not None:
                return _status_by_hold(conn, inp.hold_id, ctx.user_id, now)
        raise ToolRejectedError("get_booking_status", "invalid_arguments", "no lookup key")


# ---------------------------------------------------------------------- helpers
def _load_scenario(conn: Conn) -> ScenarioConfig:
    row = fetch_one(conn, "SELECT value FROM mock_config WHERE key = 'scenario'")
    return ScenarioConfig.model_validate(row["value"]) if row else ScenarioConfig()


def _schedule_slots(schedule: dict[str, Any], start: Any, end: Any) -> list[datetime]:
    slots: list[datetime] = []
    day = start
    while day <= end:
        if day.weekday() in schedule["weekdays"]:
            anchor = datetime(day.year, day.month, day.day, 12, tzinfo=LOCAL_TZ)
            slots.extend(local_slot(anchor, int(h)) for h in schedule["hours"])
        day += timedelta(days=1)
    return sorted(slots)


def _busy_slots(conn: Conn, provider_ids: list[str], now: datetime) -> set[tuple[str, datetime]]:
    rows = fetch_all(
        conn,
        "SELECT provider_id, slot FROM mock_bookings WHERE provider_id = ANY(%s) AND status = 'confirmed'"
        " UNION SELECT provider_id, slot FROM mock_holds"
        " WHERE provider_id = ANY(%s) AND status = 'active' AND expires_at > %s",
        (provider_ids, provider_ids, now),
    )
    return {(r["provider_id"], r["slot"].astimezone(UTC)) for r in rows}


def is_schedule_slot(provider: ProviderSeed | dict[str, Any], slot: datetime) -> bool:
    """True if `slot` is one of the provider's recurring local start times."""
    schedule = (
        provider["schedule"]
        if isinstance(provider, dict)
        else {
            "weekdays": provider.schedule_weekdays,
            "hours": provider.schedule_hours,
        }
    )
    local = slot.astimezone(LOCAL_TZ)
    return (
        local.weekday() in schedule["weekdays"]
        and local.hour in schedule["hours"]
        and local.minute == 0
        and local.second == 0
        and local.microsecond == 0
    )


def _lock_mock(conn: Conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (MOCK_LOCK_KEY,))


def _save_scenario(conn: Conn, scenario: ScenarioConfig) -> None:
    conn.execute("UPDATE mock_config SET value = %s WHERE key = 'scenario'", (Jsonb(scenario.model_dump(mode="json")),))


def _next_id(conn: Conn, prefix: str) -> str:
    row = fetch_one(conn, "UPDATE mock_rng SET counter = counter + 1 WHERE stream = 'ids' RETURNING counter")
    if row is None:
        raise ToolRejectedError("backend", "backend_not_seeded", "mock_rng stream 'ids' missing")
    return f"{prefix}-{int(row['counter']):06d}"


def _hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _replay(conn: Conn, tool: str, key: str, request_hash: str, now: datetime) -> dict[str, Any] | None:
    """Return the stored result for a finished key; reject a key reused for a different request."""
    row = fetch_one(conn, "SELECT * FROM mock_idempotency WHERE tool = %s AND key = %s FOR UPDATE", (tool, key))
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ToolRejectedError(tool, "idempotency_key_reused", "key already used for a different request")
    if row["phase"] not in ("committed", "terminal"):
        return None
    conn.execute(
        "UPDATE mock_idempotency SET calls = calls + 1, updated_at = %s WHERE tool = %s AND key = %s",
        (now, tool, key),
    )
    result: dict[str, Any] = row["result"]
    return result


def _remember(
    conn: Conn, *, tool: str, key: str, request_hash: str, phase: str, result: BaseModel, now: datetime
) -> None:
    """Persist the replay record in the same transaction as the effect it describes."""
    conn.execute(
        "INSERT INTO mock_idempotency (tool, key, request_hash, phase, result, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (tool, key) DO UPDATE SET phase = EXCLUDED.phase, result = EXCLUDED.result,"
        " calls = mock_idempotency.calls + 1, updated_at = EXCLUDED.updated_at",
        (tool, key, request_hash, phase, Jsonb(result.model_dump(mode="json")), now, now),
    )


def _verify_fence(
    conn: Conn, *, ctx: ToolContext, tool: str, key: str, expected: dict[str, Any], now: datetime
) -> None:
    """Backend-side fencing, inside the mutation transaction, under the session advisory lock.

    Requires: the current live claim generation; a reserved, unused dispatch attempt for this
    tool under that generation; and arguments equal to the operation's approved snapshot and key.
    """
    fence = ctx.fence
    if fence is None:
        raise ToolRejectedError(tool, "fence_required", "consequential call without an approved operation")
    try:
        verify_claim(conn, ctx.session_id, fence.holder, fence.generation, now)
    except StaleClaimError as exc:
        raise ToolRejectedError(tool, "fenced", str(exc)) from exc
    attempt = fetch_one(
        conn,
        "SELECT a.status, a.kind, a.tool_name, a.claim_generation, o.session_id, o.user_id, o.action,"
        " o.idempotency_key, o.snapshot FROM operation_attempts a JOIN operations o ON o.id = a.operation_id"
        " WHERE a.id = %s AND a.operation_id = %s",
        (fence.attempt_id, fence.operation_id),
    )
    if (
        attempt is None
        or attempt["status"] != "reserved"
        or attempt["kind"] != "dispatch"
        or attempt["tool_name"] != tool
        or int(attempt["claim_generation"]) != fence.generation
        or attempt["session_id"] != ctx.session_id
        or attempt["user_id"] != ctx.user_id
    ):
        raise ToolRejectedError(tool, "fenced", "no reserved attempt for this claim/operation")
    snapshot: dict[str, Any] = attempt["snapshot"]
    if (
        attempt["action"] != tool
        or attempt["idempotency_key"] != key
        or any(_norm(snapshot.get(k)) != _norm(v) for k, v in expected.items())
    ):
        raise ToolRejectedError(tool, "snapshot_mismatch", "arguments differ from the approved operation")


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(UTC).isoformat()
        except ValueError:
            return value
    return value


def _status_by_key(conn: Conn, key: str, user_id: str) -> GetBookingStatusOutput:
    row = fetch_one(conn, "SELECT phase, result FROM mock_idempotency WHERE tool = 'book' AND key = %s", (key,))
    if row is None or row["phase"] not in ("committed", "terminal"):
        return GetBookingStatusOutput(status="not_booked")
    result = BookOutput.model_validate(row["result"])
    owner = fetch_one(conn, "SELECT user_id FROM mock_holds WHERE id = %s", (result.hold_id,))
    if owner is None or owner["user_id"] != user_id:
        return GetBookingStatusOutput(status="not_booked")  # never reveal another user's booking
    return _status_from_result(result)


def _status_by_hold(conn: Conn, hold_id: str, user_id: str, now: datetime) -> GetBookingStatusOutput:
    hold = fetch_one(conn, "SELECT * FROM mock_holds WHERE id = %s AND user_id = %s", (hold_id, user_id))
    if hold is None:
        raise ToolRejectedError("get_booking_status", "not_found", hold_id)
    booking = fetch_one(conn, "SELECT * FROM mock_bookings WHERE hold_id = %s", (hold_id,))
    if booking is not None:
        return GetBookingStatusOutput(
            status="booked",
            hold_id=hold_id,
            booking_id=booking["id"],
            provider_id=booking["provider_id"],
            slot=booking["slot"],
            price_inr=booking["price_inr"],
        )
    terminal = fetch_one(
        conn,
        "SELECT result FROM mock_idempotency WHERE tool = 'book' AND phase = 'terminal'"
        " AND result->>'hold_id' = %s ORDER BY created_at DESC LIMIT 1",
        (hold_id,),
    )
    if terminal is not None:
        return _status_from_result(BookOutput.model_validate(terminal["result"]))
    if now >= hold["expires_at"]:
        return GetBookingStatusOutput(status="hold_expired", hold_id=hold_id)
    return GetBookingStatusOutput(status="not_booked", hold_id=hold_id)


def _status_from_result(result: BookOutput) -> GetBookingStatusOutput:
    if result.status == "confirmed":
        return GetBookingStatusOutput(
            status="booked",
            hold_id=result.hold_id,
            booking_id=result.booking_id,
            provider_id=result.provider_id,
            slot=result.slot,
            price_inr=result.price_inr,
        )
    return GetBookingStatusOutput(status=result.status, hold_id=result.hold_id)
