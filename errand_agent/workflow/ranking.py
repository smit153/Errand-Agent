"""Deterministic filtering and ranking of provider options (guardrail G4).

Hard constraints first (budget, date window, time window, an explicitly chosen provider), then a
stable order: a saved preferred provider first (only if it passed every filter), then price
ascending, rating descending, provider ID, slot. The model may explain the
order but can never change eligibility or ordering. Exclusion counts let code explain a no-match.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from errand_agent.tools.models import ProviderSummary
from errand_agent.workflow.parsing import LOCAL_TZ, Budget, DateWindow, TimeWindow

MAX_OPTIONS = 3


class RankedOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_no: int
    provider_id: str
    provider_name: str
    slot: datetime
    price_inr: int
    rating: float


class Ranking(BaseModel):
    model_config = ConfigDict(frozen=True)

    options: list[RankedOption]
    excluded_over_budget: int = 0
    excluded_no_slot: int = 0
    excluded_not_preferred: int = 0


def slot_fits(slot: datetime, window: DateWindow, times: TimeWindow | None) -> bool:
    local = slot.astimezone(LOCAL_TZ)
    if not window.start <= local.date() <= window.end:
        return False
    return times is None or times.admits(local.time())


def rank(
    providers: list[ProviderSummary],
    *,
    window: DateWindow,
    times: TimeWindow | None,
    budget: Budget | None,
    preferred_provider_id: str | None = None,
    favoured_provider_id: str | None = None,
    limit: int = MAX_OPTIONS,
) -> Ranking:
    """`preferred_provider_id` restricts to one provider (the user asked for them);
    `favoured_provider_id` (a saved preference) only moves that provider first if it passes every filter."""
    over_budget = no_slot = not_preferred = 0
    picks: list[tuple[int, float, str, datetime, ProviderSummary]] = []
    for p in providers:
        if preferred_provider_id is not None and p.provider_id != preferred_provider_id:
            not_preferred += 1
            continue
        if budget is not None and budget.max_inr is not None and p.price_inr > budget.max_inr:
            over_budget += 1
            continue
        slots = sorted(s for s in p.available_slots if slot_fits(s, window, times))
        if not slots:
            no_slot += 1
            continue
        picks.append((p.price_inr, -p.rating, p.provider_id, slots[0], p))
    picks.sort(key=lambda t: (t[2] != favoured_provider_id, t[0], t[1], t[2], t[3]))
    options = [
        RankedOption(
            option_no=i + 1,
            provider_id=p.provider_id,
            provider_name=p.name,
            slot=slot,
            price_inr=p.price_inr,
            rating=p.rating,
        )
        for i, (_, _, _, slot, p) in enumerate(picks[:limit])
    ]
    return Ranking(
        options=options,
        excluded_over_budget=over_budget,
        excluded_no_slot=no_slot,
        excluded_not_preferred=not_preferred,
    )


def rank_reschedule_slots(
    provider: ProviderSummary, *, window: DateWindow, original: datetime, limit: int = MAX_OPTIONS
) -> list[RankedOption]:
    """Same provider, new day: prefer the slot closest to the original time of day, then earliest."""
    orig = original.astimezone(LOCAL_TZ)
    orig_minutes = orig.hour * 60 + orig.minute

    def distance(s: datetime) -> int:
        local = s.astimezone(LOCAL_TZ)
        return abs(local.hour * 60 + local.minute - orig_minutes)

    slots = sorted(
        (s for s in provider.available_slots if slot_fits(s, window, None) and s != original),
        key=lambda s: (distance(s), s),
    )
    return [
        RankedOption(
            option_no=i + 1,
            provider_id=provider.provider_id,
            provider_name=provider.name,
            slot=s,
            price_inr=provider.price_inr,
            rating=provider.rating,
        )
        for i, s in enumerate(slots[:limit])
    ]
