"""One-line summaries of trace events for the live trace pane (the raw JSON stays available)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from errand_agent.workflow.parsing import LOCAL_TZ


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _context(ctx: dict[str, Any]) -> str:
    """What reached the model: request size against the cap, which memories, how many reviews."""
    text = f"{ctx.get('request_bytes')}/{ctx.get('cap_bytes')} B"
    sections = (ctx.get("plan") or {}).get("sections") or {}
    mem = sections.get("memories")
    if mem:
        ids = ",".join(mem.get("included") or []) or "none"
        text += f"; memories {ids} ({len(mem.get('included') or [])}/{mem.get('offered')})"
    rev = sections.get("reviews")
    if rev:
        text += f"; reviews {len(rev.get('included') or [])}/{rev.get('offered')} as untrusted data"
    return text


def summary(kind: str, p: dict[str, Any]) -> str:
    if kind == "agent.user_message":
        return f'user: "{_short(p.get("text", ""), 80)}"'
    if kind == "agent.reply":
        msgs = p.get("messages") or [""]
        return f"reply ({p.get('exit')}, {p.get('state')}): {_short(msgs[-1], 70)}"
    if kind == "state.transition":
        return f"{p.get('from')} → {p.get('to')}  ({_short(p.get('reason', ''), 50)})"
    if kind == "model.call":
        usage = p.get("usage") or {}
        verdict = f"accepted {p.get('accepted')}" if p.get("accepted") else f"REJECTED: {_short(p.get('problem'), 50)}"
        return (
            f"{p.get('purpose')} #{p.get('attempt')} {verdict}; {p.get('latency_ms')} ms; "
            f"tokens {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}; {_context(p.get('context') or {})}"
        )
    if kind == "tool.attempt_started":
        return f"→ {p.get('tool')}({_short(p.get('args'), 70)})"
    if kind == "tool.call":
        outcome = p.get("error") or p.get("result")
        mark = "ERROR " if p.get("error") else ""
        return f"← {p.get('tool')} {mark}{_short(outcome, 70)}  {p.get('latency_ms')} ms"
    if kind == "guardrail.snapshot_check":
        return "proposal matches approved card" if p.get("accepted") else f"proposal REJECTED: {p.get('problem')}"
    if kind in ("run.started", "run.ended"):
        return _short({k: p[k] for k in ("exit_reason", "counters", "holder") if k in p})
    if kind.startswith("operation.") or kind.startswith("approval.") or kind.startswith("proposal."):
        keep = (
            "action",
            "idempotency_key",
            "status",
            "result_code",
            "effect",
            "verified",
            "booking_id",
            "outcome",
            "revision",
            "form",
            "reason",
            "ordinal",
        )
        return _short({k: p[k] for k in keep if k in p and p[k] is not None})
    if kind.startswith("memory."):
        return _short({k: p[k] for k in ("display_no", "text", "status", "query", "items", "unavailable") if k in p})
    return _short(p)


def line(event_id: int, at: datetime, session_id: int | None, kind: str, payload: dict[str, Any]) -> str:
    when = at.astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    return f"{event_id:>6} {when} s{session_id or '-'} {kind:<27} {summary(kind, payload)}"
