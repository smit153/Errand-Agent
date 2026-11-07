"""Grounding scanner for model-written text (guardrail G6).

Before any model text is shown, every money amount, number, provider name or ID, weekday/month
and status word must be on an allow-list built from current state for that reply's purpose.
Status words are never allowed in model text — code templates own status (G5) — so even a
correct or negated claim ("not booked yet") fails. The scanner is a safety net; consequential
details never rely on it.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_MONEY = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:rupees|rs\b|inr\b)", re.I)
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_IDS = re.compile(r"\b(?:[A-Z]{3}-[A-Z]{3}-\d{2}|BKX?-[A-Z0-9]+|HLD-\d+|op-[0-9a-f]{8,})\b", re.I)
_DATES = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|today|tomorrow|tonight)\b",
    re.I,
)
_STATUS = re.compile(
    r"\b(booked|confirmed|cancell?ed|rescheduled|reserved|successful(?:ly)?|succeeded|completed|"
    r"all set|on hold|is held|has been (?:booked|scheduled|placed))\b",
    re.I,
)


class GroundingFacts(BaseModel):
    """What a model reply may mention. Defaults allow nothing factual."""

    model_config = ConfigDict(frozen=True)

    money_inr: frozenset[int] = Field(default_factory=frozenset)
    numbers: frozenset[str] = Field(default_factory=frozenset)
    provider_ids: frozenset[str] = Field(default_factory=frozenset)
    provider_names: frozenset[str] = Field(default_factory=frozenset)
    known_provider_names: frozenset[str] = Field(default_factory=frozenset)  # whole catalog, to detect mentions
    date_words: frozenset[str] = Field(default_factory=frozenset)


def scan(text: str, facts: GroundingFacts) -> list[str]:
    violations: list[str] = []
    remaining = text
    for m in _MONEY.finditer(text):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            violations.append(f"unreadable amount {m.group(0)!r}")
            continue
        if amount != int(amount) or int(amount) not in facts.money_inr:
            violations.append(f"amount {m.group(0).strip()!r} is not a current fact")
        remaining = remaining.replace(m.group(0), " ")
    for m in _IDS.finditer(remaining):
        if m.group(0).upper() not in {i.upper() for i in facts.provider_ids}:
            violations.append(f"identifier {m.group(0)!r} is not a current fact")
        remaining = remaining.replace(m.group(0), " ")
    for m in _NUMBER.finditer(remaining):
        if m.group(0) not in facts.numbers:
            violations.append(f"number {m.group(0)!r} is not a current fact")
    lowered = text.lower()
    allowed_names = {n.lower() for n in facts.provider_names}
    for name in facts.known_provider_names:
        if name.lower() in lowered and name.lower() not in allowed_names:
            violations.append(f"provider {name!r} is not a current option")
    allowed_dates = {d.lower() for d in facts.date_words}
    for m in _DATES.finditer(text):
        if m.group(0).lower() not in allowed_dates:
            violations.append(f"date word {m.group(0)!r} is not a current fact")
    for m in _STATUS.finditer(text):
        violations.append(f"status claim {m.group(0)!r} is reserved for code templates")
    return violations
