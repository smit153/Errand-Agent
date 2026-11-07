"""Stage 4a (G3/G4; E35, E36 and the parsing side of E01–E03): code resolves dates and ranks providers."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from errand_agent.tools.models import ProviderSummary
from errand_agent.workflow.parsing import (
    LOCAL_TZ,
    Budget,
    Clarification,
    DateWindow,
    TimeWindow,
    area_supported_by,
    occurs_in,
    resolve_budget,
    resolve_date,
    resolve_time,
    service_supported_by,
)
from errand_agent.workflow.ranking import rank, rank_reschedule_slots
from tests.conftest import T0  # Friday 6 Nov 2026, 08:00 IST


def ist(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


# ------------------------------------------------------------------ dates (G3, E35)
@pytest.mark.parametrize(
    ("expr", "start", "end"),
    [
        ("this week", date(2026, 11, 6), date(2026, 11, 8)),
        ("This week.", date(2026, 11, 6), date(2026, 11, 8)),
        ("Saturday", date(2026, 11, 7), date(2026, 11, 7)),
        ("on saturday", date(2026, 11, 7), date(2026, 11, 7)),
        ("friday", date(2026, 11, 6), date(2026, 11, 6)),  # today counts
        ("thursday", date(2026, 11, 12), date(2026, 11, 12)),  # next non-past occurrence
        ("tomorrow", date(2026, 11, 7), date(2026, 11, 7)),
        ("next week", date(2026, 11, 9), date(2026, 11, 15)),
        ("this weekend", date(2026, 11, 7), date(2026, 11, 8)),
        ("2026-11-12", date(2026, 11, 12), date(2026, 11, 12)),
        ("12 Nov", date(2026, 11, 12), date(2026, 11, 12)),
        ("november 20th", date(2026, 11, 20), date(2026, 11, 20)),
    ],
)
def test_date_expressions_resolve_against_the_injected_clock(expr: str, start: date, end: date) -> None:
    assert resolve_date(expr, T0) == DateWindow(start=start, end=end, source=expr)


@pytest.mark.parametrize(
    "expr", ["next saturday", "2026-11-01", "2026-02-30", "whenever", "soon", "1 Jan", "in two months"]
)
def test_ambiguous_past_invalid_or_unsupported_dates_clarify(expr: str) -> None:
    result = resolve_date(expr, T0)
    assert isinstance(result, Clarification)
    assert result.field == "date"


def test_today_follows_the_user_timezone_across_midnight() -> None:
    before_midnight = ist(2026, 11, 6, 23, 59).astimezone(UTC)
    after_midnight = ist(2026, 11, 7, 0, 1).astimezone(UTC)
    assert before_midnight.date() == after_midnight.date()  # same UTC day...
    assert resolve_date("today", before_midnight) == DateWindow(
        start=date(2026, 11, 6), end=date(2026, 11, 6), source="today"
    )
    assert resolve_date("today", after_midnight) == DateWindow(
        start=date(2026, 11, 7), end=date(2026, 11, 7), source="today"
    )


def test_this_week_on_sunday_night_is_just_sunday() -> None:
    sunday_late = ist(2026, 11, 8, 23, 30)
    assert resolve_date("this week", sunday_late) == DateWindow(
        start=date(2026, 11, 8), end=date(2026, 11, 8), source="this week"
    )
    assert resolve_date("this weekend", sunday_late) == DateWindow(
        start=date(2026, 11, 8), end=date(2026, 11, 8), source="this weekend"
    )


# ------------------------------------------------------------------ times
@pytest.mark.parametrize(
    ("expr", "earliest", "latest"),
    [
        ("mornings only", time(9), time(12)),
        ("not before 10 a.m.", time(10), None),
        ("not before 10am", time(10), None),
        ("after 2pm", time(14), None),
        ("before 11 am", None, time(11)),
        ("between 10 and 1", time(10), time(13)),
        ("mornings only, not before 10", time(10), time(12)),
        ("evening but before 6", time(17), time(18)),
        ("10:30 or later", time(10, 30), None),
    ],
)
def test_time_expressions(expr: str, earliest: time | None, latest: time | None) -> None:
    assert resolve_time(expr, T0) == TimeWindow(earliest=earliest, latest=latest, source=expr)


def test_not_before_10_includes_10() -> None:
    window = resolve_time("not before 10 a.m.", T0)
    assert isinstance(window, TimeWindow)
    assert window.admits(time(10))
    assert not window.admits(time(9, 59))


@pytest.mark.parametrize("expr", ["morning, after 1pm", "at a good time", "between 3pm and 1pm", "after 25"])
def test_impossible_or_unreadable_times_clarify(expr: str) -> None:
    assert isinstance(resolve_time(expr, T0), Clarification)


# ------------------------------------------------------------------ budget
@pytest.mark.parametrize(
    ("expr", "max_inr"),
    [
        ("under ₹1,500", 1499),
        ("below 1500", 1499),
        ("max 1500", 1500),
        ("up to Rs. 2,000", 2000),
        ("1.5k", 1500),
        ("₹900", 900),
        ("budget is 1200 rupees", 1200),
        ("no budget", None),
        ("price doesn't matter", None),
    ],
)
def test_budget_expressions(expr: str, max_inr: int | None) -> None:
    assert resolve_budget(expr) == Budget(max_inr=max_inr, source=expr)


@pytest.mark.parametrize("expr", ["cheap", "between 1000 and 2000", "₹0", "under ₹1.5"])
def test_unusable_budgets_clarify(expr: str) -> None:
    assert isinstance(resolve_budget(expr), Clarification)


# ------------------------------------------------------------------ spans and enum grounding
PLUMBER_REQUEST = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def test_extracted_spans_must_occur_in_the_user_text() -> None:
    assert occurs_in("this week", PLUMBER_REQUEST)
    assert occurs_in("Mornings  only", PLUMBER_REQUEST)
    assert occurs_in("under ₹1,500", PLUMBER_REQUEST)
    assert not occurs_in("tomorrow", PLUMBER_REQUEST)  # a model-invented date expression
    assert not occurs_in("under ₹2,000", PLUMBER_REQUEST)
    assert not occurs_in("", PLUMBER_REQUEST)


def test_service_and_area_need_support_in_the_user_text() -> None:
    assert service_supported_by("plumber", PLUMBER_REQUEST)
    assert not service_supported_by("electrician", PLUMBER_REQUEST)
    assert service_supported_by("ac_servicing", "Move my AC servicing to Saturday.")
    assert not service_supported_by("ac_servicing", "Accurate work please")  # 'ac' must be a whole word
    assert service_supported_by("electrician", "Book the same electrician as last time")
    assert area_supported_by("hsr_layout", "I'm in HSR")
    assert not area_supported_by("whitefield", PLUMBER_REQUEST)
    assert not service_supported_by("astrology", "astrology")


# ------------------------------------------------------------------ ranking (G4, E36)
def provider(pid: str, price: int, rating: float, *hours: int, day: int = 7) -> ProviderSummary:
    return ProviderSummary(
        provider_id=pid,
        name=f"Name {pid}",
        service="plumber",
        area="indiranagar",
        price_inr=price,
        rating=rating,
        available_slots=[ist(2026, 11, day, h).astimezone(UTC) for h in hours],
        attributes={},
    )


WEEK = DateWindow(start=date(2026, 11, 6), end=date(2026, 11, 8), source="this week")
MORNINGS = TimeWindow(earliest=time(9), latest=time(12), source="mornings only")
BUDGET = Budget(max_inr=1499, source="under ₹1,500")


def test_ranking_filters_hard_constraints_then_orders_stably() -> None:
    providers = [
        provider("P-C", 1200, 4.9, 10),
        provider("P-A", 900, 4.1, 14, 11),  # 14:00 outside mornings; 11:00 fits
        provider("P-B", 900, 4.6, 9),  # same price, better rating → ahead of P-A
        provider("P-D", 1600, 5.0, 9),  # over budget
        provider("P-E", 700, 4.0, 15),  # no morning slot
        provider("P-F", 900, 4.6, 10),  # ties P-B on price and rating → provider id order
    ]
    ranking = rank(providers, window=WEEK, times=MORNINGS, budget=BUDGET)
    assert [o.provider_id for o in ranking.options] == ["P-B", "P-F", "P-A"]
    assert [o.option_no for o in ranking.options] == [1, 2, 3]
    assert ranking.options[2].slot == ist(2026, 11, 7, 11).astimezone(UTC)  # earliest *eligible* slot
    assert ranking.excluded_over_budget == 1
    assert ranking.excluded_no_slot == 1


def test_ranking_is_independent_of_input_order() -> None:
    providers = [provider("P-1", 800, 4.5, 9), provider("P-2", 800, 4.5, 10), provider("P-3", 700, 3.0, 11)]
    forward = rank(providers, window=WEEK, times=None, budget=None)
    backward = rank(list(reversed(providers)), window=WEEK, times=None, budget=None)
    assert forward == backward
    assert [o.provider_id for o in forward.options] == ["P-3", "P-1", "P-2"]


def test_explicit_provider_restricts_candidates() -> None:
    providers = [provider("P-1", 500, 4.0, 9), provider("ELE-IND-01", 900, 4.2, 10, 11)]
    ranking = rank(
        providers,
        window=WEEK,
        times=TimeWindow(earliest=time(10), source="not before 10"),
        budget=None,
        preferred_provider_id="ELE-IND-01",
    )
    assert [o.provider_id for o in ranking.options] == ["ELE-IND-01"]
    assert ranking.options[0].slot == ist(2026, 11, 7, 10).astimezone(UTC)
    assert ranking.excluded_not_preferred == 1


def test_no_match_reports_why() -> None:
    ranking = rank([provider("P-1", 2000, 4.0, 9)], window=WEEK, times=MORNINGS, budget=BUDGET)
    assert ranking.options == []
    assert ranking.excluded_over_budget == 1


def test_reschedule_slots_prefer_the_original_time_of_day() -> None:
    p = provider("ACS-IND-01", 1000, 4.3, 9, 13, 16, day=7)
    saturday = DateWindow(start=date(2026, 11, 7), end=date(2026, 11, 7), source="Saturday")
    original = ist(2026, 11, 8, 14).astimezone(UTC)
    options = rank_reschedule_slots(p, window=saturday, original=original)
    assert [o.slot.astimezone(LOCAL_TZ).hour for o in options] == [13, 16, 9]


@pytest.mark.invariant
def test_saved_provider_moves_first_only_when_it_passes_every_filter() -> None:
    cheap = provider("PLU-IND-01", 500, 4.0, 10)
    pricier = provider("PLU-IND-02", 900, 4.5, 11)
    over_budget = provider("PLU-IND-03", 2000, 5.0, 10)
    favoured = rank([cheap, pricier], window=WEEK, times=MORNINGS, budget=BUDGET, favoured_provider_id="PLU-IND-02")
    assert [o.provider_id for o in favoured.options] == ["PLU-IND-02", "PLU-IND-01"]
    assert [o.option_no for o in favoured.options] == [1, 2]
    # A saved preference never overrides a hard filter: the over-budget provider stays excluded.
    filtered = rank([cheap, over_budget], window=WEEK, times=MORNINGS, budget=BUDGET, favoured_provider_id="PLU-IND-03")
    assert [o.provider_id for o in filtered.options] == ["PLU-IND-01"]
    assert filtered.excluded_over_budget == 1
