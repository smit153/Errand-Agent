"""Deterministic resolution of user-supplied expressions (guardrail G3).

The model only *extracts* source expressions ("this week", "mornings only", "under ₹1,500").
Code checks each expression actually occurs in the user's message, then resolves it here against
an injected clock and the user's timezone. Anything ambiguous, past or unsupported becomes a
clarification, never a guess. The model never computes a date, time or amount.

Conventions (DECISIONS §8): "this week" = today through Sunday; a weekday name = its next
non-past occurrence (today counts); mornings = [09:00, 12:00); "not before 10" includes 10:00.
"under/below/less than X" is strict (≤ X−1); "max/up to/within/budget X" is inclusive.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

LOCAL_TZ = ZoneInfo("Asia/Kolkata")
MAX_WINDOW_DAYS = 31

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class DateWindow(_Frozen):
    start: date
    end: date  # inclusive
    source: str


class TimeWindow(_Frozen):
    earliest: time | None = None  # inclusive
    latest: time | None = None  # exclusive: a slot must start before this
    source: str

    def admits(self, t: time) -> bool:
        return (self.earliest is None or t >= self.earliest) and (self.latest is None or t < self.latest)


class Budget(_Frozen):
    max_inr: int | None  # None = explicitly no limit
    source: str


class Clarification(_Frozen):
    field: Literal["service", "area", "date", "time", "budget", "provider", "booking"]
    reason: str


# ------------------------------------------------------------------ grounding of extracted spans
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def occurs_in(expression: str, user_text: str) -> bool:
    """The model's extracted expression must literally occur in what the user wrote."""
    expr = _norm(expression)
    return bool(expr) and expr in _norm(user_text)


_SERVICE_WORDS: dict[str, tuple[str, ...]] = {
    "plumber": ("plumb", "tap", "leak", "pipe", "faucet", "drain", "sink", "toilet", "flush"),
    "electrician": ("electric", "wiring", "switch", "socket", "fan", "light", "mcb", "power"),
    "ac_servicing": ("ac", "a/c", "air condition", "aircon", "air-condition"),
    "carpenter": ("carpent", "wood", "furniture", "cabinet", "shelf", "wardrobe"),
    "cleaning": ("clean", "maid", "housekeep"),
}
_AREA_WORDS: dict[str, tuple[str, ...]] = {
    "indiranagar": ("indiranagar", "indira nagar"),
    "koramangala": ("koramangala",),
    "hsr_layout": ("hsr layout", "hsr"),
    "whitefield": ("whitefield",),
    "jayanagar": ("jayanagar", "jaya nagar"),
}


def _mentions(text: str, words: tuple[str, ...]) -> bool:
    t = _norm(text)
    for w in words:
        # Short tokens ("ac", "hsr") must be whole words; longer stems may prefix ("plumb" → plumber).
        pattern = rf"\b{re.escape(w)}\b" if len(w) <= 3 else rf"\b{re.escape(w)}"
        if re.search(pattern, t):
            return True
    return False


def service_supported_by(service: str, user_text: str) -> bool:
    words = _SERVICE_WORDS.get(service)
    return words is not None and _mentions(user_text, words)


def area_supported_by(area: str, user_text: str) -> bool:
    words = _AREA_WORDS.get(area)
    return words is not None and _mentions(user_text, words)


# ------------------------------------------------------------------ dates
def today_local(now: datetime) -> date:
    return now.astimezone(LOCAL_TZ).date()


def resolve_date(expression: str, now: datetime) -> DateWindow | Clarification:
    today = today_local(now)
    expr = _norm(expression).rstrip(".!?")
    expr = re.sub(r"^(on|for|by|this coming|coming)\s+", "", expr)
    expr = re.sub(r"\s+only$", "", expr)

    def window(start: date, end: date) -> DateWindow | Clarification:
        return _window(start, end, today, expression)

    if expr == "today":
        return window(today, today)
    if expr == "tomorrow":
        return window(today + timedelta(days=1), today + timedelta(days=1))
    if expr in ("day after tomorrow", "the day after tomorrow"):
        return window(today + timedelta(days=2), today + timedelta(days=2))
    if expr in ("this week", "the rest of this week", "rest of the week", "within this week"):
        return window(today, today + timedelta(days=6 - today.weekday()))
    if expr == "next week":
        monday = today + timedelta(days=7 - today.weekday())
        return window(monday, monday + timedelta(days=6))
    if expr in ("this weekend", "weekend", "the weekend"):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:  # Sunday: the rest of this weekend is today
            return window(today, today)
        return window(saturday, saturday + timedelta(days=1))

    m = re.fullmatch(r"(this |next )?(" + "|".join(_WEEKDAYS) + r")", expr)
    if m:
        if m.group(1) == "next ":
            return Clarification(field="date", reason=f"'{expression}' could mean this or next week's {m.group(2)}")
        target = _WEEKDAYS.index(m.group(2))
        day = today + timedelta(days=(target - today.weekday()) % 7)
        return window(day, day)

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", expr)
    if m:
        return _explicit(int(m.group(1)), int(m.group(2)), int(m.group(3)), expression, today)
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    m = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?({month_names})", expr)
    if m:
        return _explicit(today.year, _MONTHS[m.group(2)], int(m.group(1)), expression, today)
    m = re.fullmatch(rf"({month_names}) (\d{{1,2}})(?:st|nd|rd|th)?", expr)
    if m:
        return _explicit(today.year, _MONTHS[m.group(1)], int(m.group(2)), expression, today)
    return Clarification(field="date", reason=f"I couldn't work out which day '{expression}' means")


def _window(start: date, end: date, today: date, source: str) -> DateWindow | Clarification:
    if end < today:
        return Clarification(field="date", reason=f"'{source}' is in the past")
    start = max(start, today)
    if (end - start).days > MAX_WINDOW_DAYS:
        return Clarification(field="date", reason=f"'{source}' is longer than {MAX_WINDOW_DAYS} days")
    return DateWindow(start=start, end=end, source=source)


def _explicit(year: int, month: int, day: int, source: str, today: date) -> DateWindow | Clarification:
    try:
        d = date(year, month, day)
    except ValueError:
        return Clarification(field="date", reason=f"'{source}' isn't a valid date")
    return _window(d, d, today, source)


# ------------------------------------------------------------------ times
_CLOCK = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?|noon|midday"


def _clock(token: str) -> time | None:
    token = token.strip()
    if token in ("noon", "midday"):
        return time(12)
    m = re.fullmatch(_CLOCK, token)
    if not m or m.group(1) is None:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    meridiem = (m.group(3) or "").replace(".", "").replace(" ", "")
    if minute > 59 or hour > 23:
        return None
    if meridiem == "am":
        hour = 0 if hour == 12 else hour
    elif meridiem == "pm":
        hour = hour if hour == 12 else hour + 12
    elif 1 <= hour <= 7:
        hour += 12  # bare "3" in a service context means 3 p.m.; 8–12 read as morning/noon
    if hour > 23:
        return None
    return time(hour, minute)


_PERIODS = {
    "morning": (time(9), time(12)),
    "afternoon": (time(12), time(17)),
    "evening": (time(17), time(21)),
}


def resolve_time(expression: str, now: datetime) -> TimeWindow | Clarification:
    del now  # times are wall-clock local; kept for a uniform resolver signature
    expr = _norm(expression).rstrip(".!?")
    earliest: time | None = None
    latest: time | None = None
    clauses = [c.strip() for c in re.split(r",|;|\bbut\b|\band\b(?!\s*\d)", expr) if c.strip()]
    if not clauses:
        return Clarification(field="time", reason="no time preference found")
    for raw_clause in clauses:
        clause = re.sub(r"\b(only|please|ideally|preferably|in the)\b", "", raw_clause).strip()
        period = next((p for p in _PERIODS if re.search(rf"\b{p}s?\b", clause)), None)
        if period is not None:
            lo, hi = _PERIODS[period]
            earliest = max(earliest or lo, lo)
            latest = min(latest or hi, hi)
            continue
        m = re.fullmatch(rf"between ({_CLOCK}) and ({_CLOCK})", clause)
        if m:
            lo_t, hi_t = _clock(m.group(1)), _clock(m.group(5))
            if lo_t is None or hi_t is None or hi_t <= lo_t:
                return Clarification(field="time", reason=f"I couldn't read the time range '{expression}'")
            earliest, latest = max(earliest or lo_t, lo_t), min(latest or hi_t, hi_t)
            continue
        m = re.fullmatch(
            rf"(not before|no earlier than|after|from|starting|starting at|at or after) ({_CLOCK})", clause
        )
        if m or re.fullmatch(rf"({_CLOCK}) (or later|onwards|or after)", clause):
            token = m.group(2) if m else clause.rsplit(" ", 2)[0]
            t = _clock(token)
            if t is None:
                return Clarification(field="time", reason=f"I couldn't read the time in '{expression}'")
            earliest = max(earliest or t, t)
            continue
        m = re.fullmatch(rf"(before|by|no later than|until|till|not after) ({_CLOCK})", clause)
        if m:
            t = _clock(m.group(2))
            if t is None:
                return Clarification(field="time", reason=f"I couldn't read the time in '{expression}'")
            latest = min(latest or t, t)
            continue
        return Clarification(field="time", reason=f"I couldn't work out the time preference '{expression}'")
    if earliest is not None and latest is not None and latest <= earliest:
        return Clarification(field="time", reason=f"'{expression}' leaves no time of day available")
    return TimeWindow(earliest=earliest, latest=latest, source=expression)


# ------------------------------------------------------------------ budget
_NO_LIMIT = re.compile(r"\b(no (budget|limit)|any (budget|price)|doesn'?t matter|no max(imum)?)\b")
_AMOUNT = re.compile(
    r"(?P<op>under|below|less than|max(?:imum)?|up ?to|within|at most|no more than|budget(?: of| is)?)?"
    r"\s*(?:₹|rs\.?|inr)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k)?\b\s*(?:rupees|rs|inr)?"
)


def resolve_budget(expression: str) -> Budget | Clarification:
    expr = _norm(expression)
    if _NO_LIMIT.search(expr):
        return Budget(max_inr=None, source=expression)
    matches = [m for m in _AMOUNT.finditer(expr) if m.group("num")]
    if len(matches) != 1:
        return Clarification(field="budget", reason=f"I couldn't read a single budget amount in '{expression}'")
    m = matches[0]
    raw = m.group("num").replace(",", "")
    try:
        value = float(raw) * (1000 if m.group("k") else 1)
    except ValueError:
        return Clarification(field="budget", reason=f"'{expression}' isn't a number I can use")
    if value <= 0 or value != int(value) or value > 1_000_000:
        return Clarification(field="budget", reason=f"'{expression}' isn't a usable budget")
    amount = int(value)
    strict = m.group("op") in ("under", "below", "less than")
    return Budget(max_inr=amount - 1 if strict else amount, source=expression)
