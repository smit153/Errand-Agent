"""Fixed context budget for every model request (PDF §3 context management; DECISIONS §§3, 18).

The cap is 4,096 UTF-8 bytes of what the app sends: the compact JSON of the messages plus the
offered tool schemas. With byte-level BPE tokenizers each token covers at least one byte, so
this also bounds tokens without a tokenizer. ModelMux's own prompt overhead is outside the cap
and is traced separately (provider-reported prompt tokens).

A prompt is built from sections:
- required: system policy, the task, the app's current facts, the user's message. If these
  alone don't fit, nothing is sent and the user gets an explicit message (E22).
- optional, each with its own byte cap, filled in keep-priority order: saved preferences
  (best-ranked first) and then untrusted review excerpts. So when space runs out, reviews are
  dropped first, then the lowest-ranked memories (DECISIONS §3 drop order).
- a fixed reserve for one retry diagnostic, so a repair request never exceeds the cap.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from errand_agent.errors import ErrandError
from errand_agent.llm.types import Message

CAP_BYTES = 4096
RETRY_RESERVE_BYTES = 600  # one diagnostic message: fixed text + ≤300-byte problem excerpt + JSON framing
DIAGNOSTIC_PROBLEM_BYTES = 300
MEMORY_SECTION_BYTES = 700
REVIEW_SECTION_BYTES = 900
REVIEW_EXCERPT_CHARS = 300


class ContextOverflowError(ErrandError):
    user_message = (
        "That message is longer than I can safely handle in one go, so I haven't done anything. "
        "Please send a shorter message."
    )


def request_bytes(messages: Sequence[Message], tool_specs: Sequence[dict[str, Any]]) -> int:
    """UTF-8 bytes of the compact JSON of exactly what is sent (messages and tool schemas)."""
    body = {"messages": [m.model_dump() for m in messages], "tools": list(tool_specs)}
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def truncate_bytes(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, cut on a character boundary."""
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class OptionalSection:
    """Droppable content. `items` are (key, text) in keep order: earlier items are kept first."""

    name: str
    items: Sequence[tuple[str, str]]
    max_bytes: int
    render: Callable[[list[str]], str]
    item_max_chars: int | None = None


class SectionReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    offered: int
    included: list[str]  # keys of included items, in order
    bytes: int


class ContextReport(BaseModel):
    """What was sent, logged with every model call (PDF §3: log what context was sent each turn)."""

    model_config = ConfigDict(frozen=True)

    cap_bytes: int = CAP_BYTES
    retry_reserve_bytes: int = RETRY_RESERVE_BYTES
    request_bytes: int = 0
    required_bytes: int = 0
    sections: dict[str, SectionReport] = Field(default_factory=dict)


@dataclass
class Prompt:
    messages: list[Message]
    report: ContextReport
    extra: dict[str, Any] = field(default_factory=dict)


def build(
    system: str,
    required: Sequence[str],
    optional: Sequence[OptionalSection],
    tool_specs: Sequence[dict[str, Any]],
    *,
    cap: int = CAP_BYTES,
    reserve: int = RETRY_RESERVE_BYTES,
) -> Prompt:
    limit = cap - reserve

    def assemble(blocks: list[str]) -> list[Message]:
        return [Message(role="system", content=system), Message(role="user", content="\n\n".join(blocks))]

    blocks = [b for b in required if b]
    required_bytes = request_bytes(assemble(blocks), tool_specs)
    if required_bytes > limit:
        raise ContextOverflowError(f"required context is {required_bytes} bytes; limit {limit} (cap {cap})")
    reports: dict[str, SectionReport] = {}
    for section in optional:
        kept: list[tuple[str, str]] = []
        for key, raw in section.items:
            text = raw if section.item_max_chars is None else _excerpt(raw, section.item_max_chars)
            trial = [*kept, (key, text)]
            rendered = section.render([t for _, t in trial])
            if len(rendered.encode("utf-8")) > section.max_bytes:
                break
            if request_bytes(assemble([*blocks, rendered]), tool_specs) > limit:
                break
            kept = trial
        if kept:
            blocks.append(section.render([t for _, t in kept]))
        reports[section.name] = SectionReport(
            offered=len(section.items),
            included=[k for k, _ in kept],
            bytes=len(section.render([t for _, t in kept]).encode("utf-8")) if kept else 0,
        )
    messages = assemble(blocks)
    total = request_bytes(messages, tool_specs)
    if total > limit:  # cannot happen given the checks above; never send an over-budget request
        raise ContextOverflowError(f"assembled context is {total} bytes; limit {limit}")
    return Prompt(
        messages=messages,
        report=ContextReport(
            cap_bytes=cap,
            retry_reserve_bytes=reserve,
            request_bytes=total,
            required_bytes=required_bytes,
            sections=reports,
        ),
    )


def _excerpt(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"
