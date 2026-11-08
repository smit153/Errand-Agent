"""Readable one-line summaries of trace events for the live trace pane (the raw JSON stays available
with `--raw`). Each line is: time, conversation, a plain label, then what happened in words:

    13:09:53  #7  TOOL ←    book: ERROR timeout, effect unknown  (26 ms)

`tag` groups events for colouring in the terminal; "detail" events (lease bookkeeping) are hidden
in the pane unless asked for. Free text from users and tools is shortened, never interpreted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from errand_agent.workflow.parsing import LOCAL_TZ

Summary = tuple[str, str, str]  # (tag, label, text)


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _key(value: Any) -> str:
    """Idempotency keys are long; the first characters are enough to match them up by eye."""
    text = str(value or "")
    return text if len(text) <= 12 else text[:12] + "…"


def _slot(value: Any) -> str:
    try:
        t = datetime.fromisoformat(str(value)).astimezone(LOCAL_TZ)
    except ValueError:
        return str(value)
    return f"{t:%a} {t.day} {t:%b %H:%M}"


def _n(count: Any, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _range(start: Any, end: Any) -> str:
    return _day(start) if start == end else f"{_day(start)}–{_day(end)}"


def _day(value: Any) -> str:
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{d.day} {d:%b}"


def _dur(p: dict[str, Any]) -> str:
    ms = p.get("latency_ms")
    if ms is None:
        return ""
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _ms(p: dict[str, Any]) -> str:
    return f"  ({_dur(p)})" if _dur(p) else ""


# ------------------------------------------------------------------ tools
def _tool_args(tool: str, a: dict[str, Any]) -> str:
    if tool == "search_providers":
        dr = a.get("date_range") or {}
        return f"{a.get('service')}, {a.get('area')}, {_range(dr.get('start'), dr.get('end'))}"
    if tool == "hold_slot":
        return f"{a.get('provider_id')}, {_slot(a.get('slot'))}"
    if tool == "book":
        return f"hold {a.get('hold_id')}, key {_key(a.get('idempotency_key'))}"
    if tool == "get_booking_status":
        return f"key {_key(a.get('idempotency_key'))}" if a.get("idempotency_key") else f"hold {a.get('hold_id')}"
    if tool in ("cancel", "reschedule"):
        slot = f", to {_slot(a['slot'])}" if a.get("slot") else ""
        return f"{a.get('booking_id')}{slot}, key {_key(a.get('idempotency_key'))}"
    return ", ".join(str(v) for v in a.values())


def _tool_result(tool: str, r: dict[str, Any]) -> str:
    if tool == "search_providers":
        return f"{len(r.get('providers') or [])} providers found"
    if tool == "get_provider":
        return f"{r.get('name')} ★{r.get('rating')}, {len(r.get('reviews') or [])} reviews (third-party text)"
    if tool == "hold_slot":
        expires = _slot(r.get("expires_at")).split()[-1]
        return f"held {r.get('hold_id')} at ₹{r.get('quoted_price_inr')} until {expires}"
    if tool == "book":
        return f"{r.get('status')} {r.get('booking_id') or ''}".strip()
    if tool == "get_booking_status":
        return f"{r.get('status')} {r.get('booking_id') or ''}".strip()
    if tool == "list_bookings":
        return f"{len(r.get('bookings') or [])} bookings"
    if tool in ("cancel", "reschedule"):
        return f"{r.get('status')} {r.get('booking_id') or ''}".strip()
    return _short(r, 70)


def _tool_call(p: dict[str, Any]) -> Summary:
    tool = str(p.get("tool"))
    err = p.get("error")
    if err:
        return "error", "TOOL ←", f"{tool}: ERROR {err.get('code')}, effect {err.get('effect')}{_ms(p)}"
    return "tool", "TOOL ←", f"{tool}: {_tool_result(tool, p.get('result') or {})}{_ms(p)}"


# ------------------------------------------------------------------ model
PURPOSE = {"extract": "read the message", "act": "pick next step", "phrase": "word the reply"}


def _context(ctx: dict[str, Any]) -> str:
    """What reached the model: request size against the cap, which memories, how many reviews."""
    text = f"sent {ctx.get('request_bytes')} of {ctx.get('cap_bytes')} bytes"
    sections = (ctx.get("plan") or {}).get("sections") or {}
    mem = sections.get("memories")
    if mem:
        ids = " ".join(mem.get("included") or []) or "none"
        text += f"; memories {ids}"
    rev = sections.get("reviews")
    if rev:
        text += f"; {len(rev.get('included') or [])} reviews as untrusted data"
    return text


def _model_call(p: dict[str, Any]) -> Summary:
    usage = p.get("usage") or {}
    purpose = str(p.get("purpose"))
    head = f"{purpose} ({PURPOSE.get(purpose, purpose)}) #{p.get('attempt')}: "
    tokens = f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out tokens"
    ctx = (p.get("context") or {}) if isinstance(p.get("context"), dict) else {}
    tail = f"  ·  {_dur(p)}  ·  {tokens}\n{_context(ctx)}"
    if p.get("accepted"):
        return "model", "MODEL", f"{head}proposed {p['accepted']} ✓{tail}"
    problem = str(p.get("problem") or "").split(": ", 1)[-1]  # drop the "update_request: " prefix
    return "error", "MODEL", f"{head}REJECTED, {_short(problem, 60)}{tail}"


# ------------------------------------------------------------------ conversation and workflow
def _request_updated(p: dict[str, Any]) -> Summary:
    req = p.get("request") or {}
    parts = []
    for name in p.get("changed") or []:
        value = req.get(name)
        if name == "date" and isinstance(value, dict):
            value = _range(value.get("start"), value.get("end"))
        elif name == "time" and isinstance(value, dict):
            value = f"{(value.get('earliest') or '')[:5]}–{(value.get('latest') or '')[:5]}"
        elif name == "budget" and isinstance(value, dict):
            value = f"up to ₹{value.get('max_inr')}" if value.get("max_inr") else "no limit"
        parts.append(f"{name} = {value}" if value is not None else name)
    text = "changed: " + "; ".join(parts) if parts else "nothing changed"
    if p.get("clarifications"):
        text += f"; needs: {_short(p['clarifications'], 60)}"
    return "agent", "REQUEST", text


def _reply(p: dict[str, Any]) -> Summary:
    msgs = p.get("messages") or [""]
    first = next((ln for ln in str(msgs[-1]).splitlines() if ln.strip()), "")
    return "agent", "AGENT", f"[{p.get('state')}] {_short(first, 80)}"


def _approval(kind: str, p: dict[str, Any]) -> Summary:
    if kind == "approval.card_displayed":
        return "approval", "APPROVAL", f"card shown (revision {p.get('revision')}): waiting for 'yes' or its code"
    if kind == "proposal.created":
        return "approval", "APPROVAL", f"proposal: {p.get('action')} (revision {p.get('revision')})"
    if kind == "proposal.invalidated":
        return "approval", "APPROVAL", f"card no longer valid: {_short(p.get('reason'), 60)}"
    outcome = p.get("outcome")
    if outcome == "approved":
        return "approval", "APPROVAL", f"approved by the user ('{p.get('form')}'), checked by code"
    if outcome == "duplicate":
        return "approval", "APPROVAL", "already approved: same operation, nothing new is sent"
    return "error", "APPROVAL", f"not approved ({outcome}): {p.get('reason')}"


def _operation(kind: str, p: dict[str, Any]) -> Summary:
    key = _key(p.get("idempotency_key"))
    if kind == "operation.created":
        return "operation", "BOOKING", f"{p.get('action')} operation created, idempotency key {key}"
    if kind == "operation.dispatch_reserved":
        return "operation", "BOOKING", f"send #{p.get('ordinal')} reserved (key {key})"
    if kind == "operation.dispatch_recorded":
        effect = p.get("effect")
        said = {"unknown": "no answer, so the effect is unknown", "returned": "the provider answered"}.get(str(effect))
        return "operation", "BOOKING", said or f"provider answer: {effect}"
    if kind == "operation.reconciled":
        return "operation", "BOOKING", "outcome confirmed with the source of truth"
    if kind == "operation.reconcile_scheduled":
        return "operation", "BOOKING", "status check scheduled (source of truth unreachable)"
    if kind == "operation.finished":
        mark = "✔" if p.get("status") == "succeeded" else "✖"
        verified = "verified" if p.get("verified") else "NOT verified"
        bk = f" {p['booking_id']}" if p.get("booking_id") else ""
        return "operation", "BOOKING", f"{mark} {p.get('status')}: {p.get('result_code')}, {verified}{bk}"
    return "operation", "BOOKING", f"{kind.split('.', 1)[1]}: {_short(p, 70)}"


def _memory(kind: str, p: dict[str, Any]) -> Summary:
    if kind == "memory.retrieved":
        items = " ".join(f"#{i.get('display_no')}" for i in p.get("items") or []) or "none"
        extra = " (memory unavailable)" if p.get("unavailable") else ""
        return "memory", "MEMORY", f'relevant to "{_short(p.get("query"), 40)}": {items}{extra}'
    if kind == "memory.defaults_applied":
        facts = p.get("facts") or []
        used = "; ".join(f"#{f.get('display_no')} {f.get('text')}" for f in facts) or "none"
        return "memory", "MEMORY", f"saved defaults used: {_short(used, 80)}"
    action = kind.split(".", 1)[1].replace("_", " ")
    no = p.get("display_no")
    text = f": {_short(p.get('text'), 60)}" if p.get("text") else ""
    return "memory", "MEMORY", f"{action} #{no}{text}" if no is not None else f"{action}{text}"


def _run(kind: str, p: dict[str, Any]) -> Summary:
    if kind == "run.started":
        lim = p.get("limits") or {}
        if not lim:
            return "run", "RUN", "── run started ──"
        return (
            "run",
            "RUN",
            (
                f"── run started · limits: {_n(lim.get('logical_model_calls'), 'model call')}, "
                f"{_n(lim.get('tool_invocations'), 'tool call')}, {lim.get('active_seconds')} s ──"
            ),
        )
    c = p.get("counters") or {}
    used = (
        f"{_n(c.get('logical_model_calls', 0), 'model call')}, {_n(c.get('tool_invocations', 0), 'tool call')}, "
        f"{_n(c.get('embedding_calls', 0), 'embedding')}"
    )
    reason = str(p.get("exit_reason"))
    if reason.startswith("job:"):
        reason = f"background job {reason[4:]}, no user message"
    return "run", "RUN", f"ended ({reason}): used {used}; cost n/a (subscription)"


SIMPLE: dict[str, Callable[[dict[str, Any]], Summary]] = {
    "agent.user_message": lambda p: ("user", "YOU", f'"{_short(p.get("text", ""), 90)}"'),
    "agent.reply": _reply,
    "agent.request_updated": _request_updated,
    "agent.memory_command": lambda p: (
        "memory",
        "MEMORY",
        f"command: {p.get('kind')}" + (f" #{p['number']}" if p.get("number") is not None else ""),
    ),
    "agent.degraded": lambda p: ("error", "AGENT", f"model unreliable, showing a menu: {_short(p.get('detail'), 60)}"),
    "agent.error": lambda p: ("error", "ERROR", f"{p.get('type')}: {_short(p.get('detail'), 70)}"),
    "model.call": _model_call,
    "tool.attempt_started": lambda p: (
        "tool",
        "TOOL →",
        f"{p.get('tool')}({_tool_args(str(p.get('tool')), p.get('args') or {})})",
    ),
    "tool.call": _tool_call,
    "state.transition": lambda p: (
        "state",
        "STATE",
        f"{p.get('from')} → {p.get('to')}  ({_short(p.get('reason', ''), 50)})",
    ),
    "guardrail.snapshot_check": lambda p: (
        ("guard", "GUARD", "✓ the model's call matches the approved card exactly")
        if p.get("accepted")
        else ("error", "GUARD", f"✗ call REJECTED, differs from the card: {_short(p.get('problem'), 60)}")
    ),
    "guardrail.grounding_violation": lambda p: ("error", "GUARD", f"✗ reply REJECTED: {_short(p, 70)}"),
    "budget.exhausted": lambda p: ("error", "LIMIT", f"limit reached: {p.get('limit')}"),
    "job.started": lambda p: ("run", "RUN", f"background job: {p.get('kind')} (no user message)"),
    "recovery.started": lambda p: ("run", "RUN", f"recovery after restart: {_short(p, 70)}"),
    "provider_event.ingested": lambda p: ("event", "EVENT", f"provider event received: {_short(p, 70)}"),
    "provider_event.handled": lambda p: (
        "event",
        "EVENT",
        f"provider cancelled {p.get('booking_id')}: {p.get('status')} (checked with list_bookings)",
    ),
    "claim.acquired": lambda p: ("detail", "LEASE", f"session lease taken by {p.get('holder')}"),
}


def summary(kind: str, p: dict[str, Any]) -> Summary:
    if kind in SIMPLE:
        return SIMPLE[kind](p)
    if kind.startswith(("approval.", "proposal.")):
        return _approval(kind, p)
    if kind.startswith("operation."):
        return _operation(kind, p)
    if kind.startswith("memory."):
        return _memory(kind, p)
    if kind.startswith("run."):
        return _run(kind, p)
    return "other", kind, _short(p)


def line(event_id: int, at: datetime, session_id: int | None, kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """(tag, text) for one event. `event_id` stays in the API response, not the text."""
    del event_id
    tag, label, text = summary(kind, payload)
    when = at.astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    conv = f"#{session_id}" if session_id else "#-"
    prefix = f"{when}  {conv:<4} {label:<9} "
    return tag, prefix + text.replace("\n", "\n" + " " * len(prefix))
