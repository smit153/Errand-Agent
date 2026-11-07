"""Stage 2a (E41 part): search_providers, get_provider, list_bookings contracts and seeding."""

from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import MAX_SLOTS_PER_PROVIDER, is_schedule_slot
from errand_agent.mocks.catalog import DEMO_USER, INJECTION_REVIEW, LOCAL_TZ, build_catalog
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.tools.errors import ToolRejectedError
from errand_agent.tools.models import AREAS, SERVICES

WEEK = {"start": "2026-11-06", "end": "2026-11-08"}


def test_catalog_is_deterministic_and_every_pair_has_3_to_8_providers() -> None:
    a, ra = build_catalog(42)
    b, rb = build_catalog(42)
    assert a == b
    assert ra == rb
    assert build_catalog(7)[0] != a
    counts = Counter((p.service, p.area) for p in a)
    assert set(counts) == {(s, ar) for s in SERVICES for ar in AREAS}
    assert all(3 <= n <= 8 for n in counts.values())


def test_every_pair_has_an_injection_review_on_its_cheapest_provider() -> None:
    providers, reviews = build_catalog(42)
    injected = {r.provider_id for r in reviews if r.body == INJECTION_REVIEW}
    for service in SERVICES:
        for area in AREAS:
            pair = [p for p in providers if p.service == service and p.area == area]
            cheapest = min(pair, key=lambda p: (p.base_price_inr, p.id))
            assert cheapest.id in injected


def test_search_returns_contract_fields_and_valid_future_slots(
    tools: ToolClient, ctx: ToolContext, clock: FakeClock
) -> None:
    out = tools.search_providers(ctx, {"service": "plumber", "area": "indiranagar", "date_range": WEEK})
    assert 3 <= len(out.providers) <= 8
    assert any(p.available_slots for p in out.providers)
    for p in out.providers:
        assert p.service == "plumber"
        assert p.area == "indiranagar"
        assert p.price_inr > 0
        assert 0 <= p.rating <= 5
        assert p.attributes
        assert len(p.available_slots) <= MAX_SLOTS_PER_PROVIDER
        assert p.available_slots == sorted(p.available_slots)
        for s in p.available_slots:
            assert s > clock.now()
            local_day = s.astimezone(LOCAL_TZ).date()
            assert date(2026, 11, 6) <= local_day <= date(2026, 11, 8)


def test_search_slots_are_on_provider_schedule(tools: ToolClient, ctx: ToolContext, db: Database) -> None:
    out = tools.search_providers(ctx, {"service": "electrician", "area": "koramangala", "date_range": WEEK})
    with db.txn() as conn:
        rows = {
            r["id"]: r
            for r in conn.execute("SELECT id, schedule FROM mock_providers WHERE service = 'electrician'").fetchall()
        }
    for p in out.providers:
        assert all(is_schedule_slot(rows[p.provider_id], s) for s in p.available_slots)


@pytest.mark.parametrize(
    "args",
    [
        {"service": "astrologer", "area": "indiranagar", "date_range": WEEK},
        {"service": "plumber", "area": "mars", "date_range": WEEK},
        {"service": "plumber", "area": "indiranagar", "date_range": {"start": "2026-11-08", "end": "2026-11-06"}},
        {"service": "plumber", "area": "indiranagar", "date_range": WEEK, "approved": True},
    ],
)
def test_search_rejects_invalid_arguments_and_traces_it(
    tools: ToolClient, ctx: ToolContext, db: Database, args: dict[str, object]
) -> None:
    with pytest.raises(ToolRejectedError) as info:
        tools.search_providers(ctx, args)
    assert info.value.code == "invalid_arguments"
    assert info.value.effect == "none"
    with db.txn() as conn:
        ev = conn.execute("SELECT payload FROM trace_events WHERE kind = 'tool.call'").fetchone()
    assert ev is not None
    assert ev["payload"]["error"]["code"] == "invalid_arguments"


def test_get_provider_returns_reviews_including_injection_as_data(tools: ToolClient, ctx: ToolContext) -> None:
    out = tools.search_providers(ctx, {"service": "plumber", "area": "indiranagar", "date_range": WEEK})
    cheapest = min(out.providers, key=lambda p: (p.price_inr, p.provider_id))
    details = tools.get_provider(ctx, {"provider_id": cheapest.provider_id})
    assert details.provider_id == cheapest.provider_id
    assert details.description
    assert any(r.text == INJECTION_REVIEW for r in details.reviews)
    assert all(1 <= r.rating <= 5 for r in details.reviews)


def test_get_provider_unknown_id_is_rejected(tools: ToolClient, ctx: ToolContext) -> None:
    with pytest.raises(ToolRejectedError) as info:
        tools.get_provider(ctx, {"provider_id": "PLU-IND-99"})
    assert info.value.code == "not_found"


def test_list_bookings_returns_history_fields(tools: ToolClient, ctx: ToolContext) -> None:
    out = tools.list_bookings(ctx, {"user_id": DEMO_USER})
    by_service = {b.service: b for b in out.bookings}
    assert {"electrician", "ac_servicing", "plumber"} <= set(by_service)
    assert by_service["electrician"].status == "completed"
    assert by_service["ac_servicing"].status == "confirmed"
    for b in out.bookings:
        assert b.booking_id
        assert b.provider_id
        assert b.provider_name


def test_list_bookings_for_another_user_is_forbidden(tools: ToolClient, ctx: ToolContext) -> None:
    with pytest.raises(ToolRejectedError) as info:
        tools.list_bookings(ctx, {"user_id": "someone-else"})
    assert info.value.code == "forbidden"


def test_every_call_is_traced_with_latency(tools: ToolClient, ctx: ToolContext, db: Database) -> None:
    tools.list_bookings(ctx, {"user_id": DEMO_USER})
    with db.txn() as conn:
        ev = conn.execute("SELECT session_id, payload FROM trace_events WHERE kind = 'tool.call'").fetchone()
    assert ev is not None
    assert ev["session_id"] == ctx.session_id
    assert ev["payload"]["tool"] == "list_bookings"
    assert ev["payload"]["latency_ms"] >= 0
    assert ev["payload"]["result"]["bookings"]


def _events(db: Database, tool: str) -> list[tuple[str, dict[str, object]]]:
    with db.txn() as conn:
        rows = conn.execute(
            "SELECT kind, payload FROM trace_events WHERE kind IN ('tool.attempt_started', 'tool.call')"
            " AND payload->>'tool' = %s ORDER BY id",
            (tool,),
        ).fetchall()
    return [(r["kind"], r["payload"]) for r in rows]


def test_attempt_started_is_written_before_the_call_with_the_same_call_id(
    tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    tools.list_bookings(ctx, {"user_id": DEMO_USER})
    (started, finished) = _events(db, "list_bookings")
    assert started[0] == "tool.attempt_started"
    assert finished[0] == "tool.call"
    assert started[1]["call_id"] == finished[1]["call_id"]
    assert "result" not in started[1]


class _CrashError(Exception):
    pass


def test_a_call_that_crashes_mid_flight_still_shows_its_start(
    tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    def crash(point: str, _tool: str, _ctx: ToolContext) -> None:
        if point == "after_backend":
            raise _CrashError

    tools.fault = crash
    with pytest.raises(_CrashError):
        tools.list_bookings(ctx, {"user_id": DEMO_USER})
    assert [kind for kind, _ in _events(db, "list_bookings")] == ["tool.attempt_started"]


def test_arguments_rejected_by_validation_never_start_an_attempt(
    tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    with pytest.raises(ToolRejectedError):
        tools.get_provider(ctx, {"provider_id": "bad id!"})
    assert [kind for kind, _ in _events(db, "get_provider")] == ["tool.call"]
