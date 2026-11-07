"""Postgres-backed mock provider backend implementing the eight fixed tool contracts.

Scenario controls (forced outcomes, quote drift, conflicts) are test/ops controls, not tools.
All time comes from the injected clock. Read-only tools take no locks beyond MVCC; mutating tools
take the session advisory lock (for fencing), then the mock-global lock, then rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from errand_agent.clock import Clock
from errand_agent.db import Conn, Database, fetch_all, fetch_one
from errand_agent.mocks.catalog import (
    DEMO_USER,
    LOCAL_TZ,
    ProviderSeed,
    build_catalog,
    build_history,
    local_slot,
)
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError
from errand_agent.tools.models import (
    BookingRecord,
    GetProviderInput,
    GetProviderOutput,
    ListBookingsInput,
    ListBookingsOutput,
    ProviderSummary,
    Review,
    SearchProvidersInput,
    SearchProvidersOutput,
)

MAX_SLOTS_PER_PROVIDER = 6
HOLD_TTL = timedelta(minutes=10)
MOCK_LOCK_KEY = -0x4D4F434B  # "MOCK"; negative so it never collides with bigserial session ids


class ScenarioConfig(BaseModel):
    """Configurable branches for reproducible scenarios. Defaults reproduce the PDF behaviour."""

    model_config = ConfigDict(extra="forbid")

    book_outcomes: list[str] = Field(default_factory=list)  # forced FIFO; see BOOK_OUTCOMES
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
