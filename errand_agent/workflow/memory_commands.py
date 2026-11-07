"""Deterministic memory commands and value parsing (PDF §3: inspect, correct, forget).

Like `yes` / `confirm <token>`, these are handled by code and never reach the model: they only
read or change the user's own memory and can't touch requests, approvals or bookings. Users
refer to facts by the number shown in the memory view. Values are parsed by the same
deterministic resolvers as requests.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.memory.facts import AreaValue, EarliestTimeValue, MemoryField, NoteValue, Scope
from errand_agent.tools.models import AREAS
from errand_agent.workflow.parsing import Clarification, area_supported_by, resolve_time

CommandKind = Literal["show", "forget", "forget_field", "confirm", "correct"]


class MemoryCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: CommandKind
    number: int | None = None
    field: MemoryField | None = None
    scope: Scope | None = None
    value: str | None = None


_END = r"\s*[.!?]*\s*"
_SHOW = re.compile(
    r"\s*(?:(?:show|list|view|see)(?: me)? (?:all )?(?:my )?(?:saved )?(?:memor(?:y|ies)|preferences)"
    r"|what do you (?:remember|know) about me|my (?:memories|saved preferences)|memories)" + _END,
    re.I,
)
_FORGET_N = re.compile(r"\s*forget (?:memory )?#?(\d{1,4})" + _END, re.I)
_CONFIRM_N = re.compile(r"\s*(?:confirm|keep) memory #?(\d{1,4})" + _END, re.I)
_CORRECT_N = re.compile(r"\s*(?:correct|change|update|set) memory #?(\d{1,4})\s*(?:to|:|=)\s*(.+?)\s*", re.I)
_SERVICE_WORDS: dict[str, Scope] = {
    "plumber": "plumber",
    "plumbing": "plumber",
    "electrician": "electrician",
    "electrical": "electrician",
    "ac": "ac_servicing",
    "ac servicing": "ac_servicing",
    "carpenter": "carpenter",
    "cleaning": "cleaning",
    "cleaner": "cleaning",
}
_FORGET_FIELD = re.compile(
    r"\s*forget (?:my )?(?:saved |preferred |usual )?"
    r"(?:(?P<area>area|location)|(?P<time>earliest time|earliest acceptable time|time preference|earliest)"
    r"|(?P<service>" + "|".join(sorted(_SERVICE_WORDS, key=len, reverse=True)) + r") (?:provider|technician))" + _END,
    re.I,
)


def parse_memory_command(text: str) -> MemoryCommand | None:
    if _SHOW.fullmatch(text):
        return MemoryCommand(kind="show")
    if m := _FORGET_N.fullmatch(text):
        return MemoryCommand(kind="forget", number=int(m.group(1)))
    if m := _CONFIRM_N.fullmatch(text):
        return MemoryCommand(kind="confirm", number=int(m.group(1)))
    if m := _CORRECT_N.fullmatch(text):
        return MemoryCommand(kind="correct", number=int(m.group(1)), value=m.group(2))
    if m := _FORGET_FIELD.fullmatch(text):
        if m.group("area"):
            return MemoryCommand(kind="forget_field", field="area", scope="global")
        if m.group("time"):
            return MemoryCommand(kind="forget_field", field="earliest_time", scope="global")
        return MemoryCommand(
            kind="forget_field", field="preferred_provider", scope=_SERVICE_WORDS[m.group("service").lower()]
        )
    return None


def parse_area(text: str) -> AreaValue | None:
    area = next((a for a in AREAS if area_supported_by(a, text)), None)
    return None if area is None else AreaValue(area=area)


def parse_earliest(text: str, now: datetime) -> EarliestTimeValue | None:
    """'10 am', 'not before 10 a.m.', '10:30 or later' → an earliest time; anything else → None."""
    for candidate in (text, f"not before {text}"):
        window = resolve_time(candidate, now)
        if not isinstance(window, Clarification) and window.earliest is not None and window.latest is None:
            return EarliestTimeValue(earliest=window.earliest)
    return None


def parse_note(text: str) -> NoteValue | None:
    stripped = text.strip()
    return NoteValue(text=stripped) if 0 < len(stripped) <= 200 else None


PROVIDER_ID = re.compile(r"\b([A-Za-z]{3}-[A-Za-z]{3}-\d{2})\b")
MEMORY_CUE = re.compile(
    r"\b(remember|from now on|always|going forward|in future|by default|keep in mind|"
    r"save (?:this|that|it|my))\b",
    re.I,
)
