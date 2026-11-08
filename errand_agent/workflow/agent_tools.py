"""State-scoped tool catalog (G1) and per-turn, enum-constrained tool definitions (G2).

`STATE_TOOLS` is the single allow-list: the agent only *offers* tools listed for the current
state, and checks the same table again before *dispatching* anything (E33). ID-like parameters
are enums of values that exist right now; a tool with an empty enum is simply not offered.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from errand_agent.errors import ErrandError
from errand_agent.llm.toolspec import ToolDef, args_model, enum_of
from errand_agent.tools.models import AREAS, SERVICES
from errand_agent.workflow.memory_commands import MEMORY_CUE
from errand_agent.workflow.parsing import area_supported_by, occurs_in, service_supported_by
from errand_agent.workflow.states import SessionState

STATE_TOOLS: dict[SessionState, frozenset[str]] = {
    "collecting": frozenset({"update_request", "ask_user", "search_providers", "list_bookings"}),
    "comparing": frozenset({"update_request", "get_provider", "select_option"}),
    "held": frozenset({"update_request", "hold_slot"}),
    "awaiting_approval": frozenset({"update_request"}),
    # Consequential tools only while an approved operation is executing; never otherwise.
    "executing": frozenset({"book", "cancel", "reschedule"}),
    "verifying": frozenset(),
    "retry_wait": frozenset(),
    "pending_verification": frozenset(),
    "conflict": frozenset({"update_request"}),
    "completed": frozenset({"update_request"}),
    "failed": frozenset({"update_request"}),
}


# Changing an existing booking is a different errand from making a new one, so the model may only
# pick those intents when the user's own words say so. "Make it next week" corrects a detail of the
# request in progress; it must not turn it into a reschedule (BUG-011). "book" needs no cue: it
# is the default and has no side effect without a card and approval.
INTENT_CUES: dict[str, re.Pattern[str]] = {
    "reschedule": re.compile(
        r"\b(move|moving|reschedul\w*|shift|postpone|prepone|push (?:it )?back|bring (?:it )?forward|"
        r"change (?:my|the) (?:\w+ )?(?:booking|appointment|slot|visit))\b",
        re.I,
    ),
    "cancel": re.compile(r"\b(cancel\w*|call(?:ing)? (?:it )?off|(?:don'?t|do not|no longer) need)\b", re.I),
}


class ToolNotAllowedError(ErrandError):
    """A programming error: code tried to offer or dispatch a tool outside the state allow-list."""


def ensure_allowed(state: SessionState, names: list[str]) -> None:
    denied = [n for n in names if n not in STATE_TOOLS[state]]
    if denied:
        raise ToolNotAllowedError(f"tools {denied} are not allowed in state {state}")


# ------------------------------------------------------------------ language extraction
def update_request_tool(user_text: str, option_numbers: list[int]) -> ToolDef:
    fields: dict[str, tuple[Any, Any]] = {
        "intent": (enum_of(["book", "reschedule", "cancel"]) | None, None),
        "service": (enum_of(list(SERVICES)) | None, None),
        "area": (enum_of(list(AREAS)) | None, None),
        "date_expression": (str | None, None),
        "time_expression": (str | None, None),
        "budget_expression": (str | None, None),
        "provider_reference": (enum_of(["same_as_last_time"]) | None, None),
        "remember": (list.__class_getitem__(enum_of(["area", "time", "provider"])) | None, None),
        "remember_note": (str | None, None),
    }
    if option_numbers:
        fields["option_number"] = (enum_of([str(n) for n in option_numbers]) | None, None)

    def grounded(args: BaseModel) -> str | None:
        data = args.model_dump()
        cue = INTENT_CUES.get(data.get("intent") or "")
        if cue is not None and not cue.search(user_text):
            return (
                f"intent is not stated in the user's message: '{data['intent']}' is only for changing an "
                "existing booking when the user asks to; leave intent null when they correct a detail"
            )
        for name in ("date_expression", "time_expression", "budget_expression"):
            value = data.get(name)
            if value is not None and not occurs_in(value, user_text):
                return f"{name} must be copied verbatim from the user's message"
        if data.get("service") and not service_supported_by(data["service"], user_text):
            return "service is not stated in the user's message"
        if data.get("area") and not area_supported_by(data["area"], user_text):
            return "area is not stated in the user's message"
        if (data.get("remember") or data.get("remember_note")) and not MEMORY_CUE.search(user_text):
            return "remember and remember_note are only for messages where the user explicitly asks you to remember"
        note = data.get("remember_note")
        if note is not None and not occurs_in(note, user_text):
            return "remember_note must be copied verbatim from the user's message"
        return None

    return ToolDef(
        "update_request",
        "Record what the user's latest message states. Copy expressions verbatim; leave unstated fields null. "
        "Use remember / remember_note only when the user explicitly asks you to remember something.",
        args_model("UpdateRequestArgs", **fields),
        grounded,
    )


def ask_user_tool(missing: list[str]) -> ToolDef:
    return ToolDef(
        "ask_user",
        "Ask the user for details that are still missing.",
        args_model("AskUserArgs", fields=(list.__class_getitem__(enum_of(missing)), ...)),
    )


# ------------------------------------------------------------------ fixed backend tools (contracts unchanged)
def search_providers_tool(service: str, area: str, start: str, end: str) -> ToolDef:
    date_range = args_model("DateRangeArgs", start=(enum_of([start]), ...), end=(enum_of([end]), ...))
    return ToolDef(
        "search_providers",
        "Search for providers for the request.",
        args_model(
            "SearchProvidersArgs",
            service=(enum_of([service]), ...),
            area=(enum_of([area]), ...),
            date_range=(date_range, ...),
        ),
    )


def get_provider_tool(provider_ids: list[str]) -> ToolDef:
    return ToolDef(
        "get_provider",
        "Get details and reviews for a provider.",
        args_model("GetProviderArgs", provider_id=(enum_of(provider_ids), ...)),
    )


def select_option_tool(option_numbers: list[int]) -> ToolDef:
    return ToolDef(
        "select_option",
        "Select the option to prepare for the user's approval.",
        args_model("SelectOptionArgs", option_no=(enum_of([str(n) for n in option_numbers]), ...)),
    )


def hold_slot_tool(provider_id: str, slot: datetime) -> ToolDef:
    return ToolDef(
        "hold_slot",
        "Hold the selected slot so its price can be confirmed.",
        args_model("HoldSlotArgs", provider_id=(enum_of([provider_id]), ...), slot=(enum_of([slot.isoformat()]), ...)),
    )


def list_bookings_tool(user_id: str) -> ToolDef:
    return ToolDef(
        "list_bookings",
        "List the user's past and upcoming bookings.",
        args_model("ListBookingsArgs", user_id=(enum_of([user_id]), ...)),
    )


def consequential_tool(action: str, args: dict[str, Any]) -> ToolDef:
    """The single approved action, with every argument an enum of its approved value."""
    fields: dict[str, tuple[Any, Any]] = {}
    for name, value in args.items():
        if name == "idempotency_key":
            fields[name] = (enum_of([str(value)]) | None, None)  # optional: code supplies the key anyway
        else:
            fields[name] = (enum_of([str(value)]), ...)
    return ToolDef(action, f"Carry out the {action} the user approved.", args_model(f"{action.title()}Args", **fields))
