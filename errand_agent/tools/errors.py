"""Tool failure types. Each states whether an effect may have happened."""

from __future__ import annotations

from typing import Literal

EffectKnowledge = Literal["none", "unknown"]


class ToolError(Exception):
    """A tool call failed. `effect` says what the caller may assume about side effects."""

    effect: EffectKnowledge = "unknown"

    def __init__(self, tool: str, code: str, detail: str = "") -> None:
        super().__init__(f"{tool}: {code}{': ' + detail if detail else ''}")
        self.tool = tool
        self.code = code
        self.detail = detail


class ToolTimeoutError(ToolError):
    """The call timed out. The effect may or may not have been committed."""

    effect: EffectKnowledge = "unknown"

    def __init__(self, tool: str, detail: str = "") -> None:
        super().__init__(tool, "timeout", detail)


class ToolTransientError(ToolError):
    """Known no-effect transient failure (e.g. HTTP 503). Safe to retry with the same key."""

    effect: EffectKnowledge = "none"

    def __init__(self, tool: str, status: int = 503, detail: str = "") -> None:
        super().__init__(tool, f"http_{status}", detail)
        self.status = status


class ToolRejectedError(ToolError):
    """Rejected before any effect: bad input, unknown/foreign id, fenced claim, key reuse."""

    effect: EffectKnowledge = "none"
