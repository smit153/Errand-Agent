"""Scripted model for offline evaluation: replays a fixed sequence of replies, records every request."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from errand_agent.llm.types import ChatRequest, ModelReply, ModelUnavailableError, RawToolCall

Step = ModelReply | Callable[[ChatRequest], ModelReply] | type[ModelUnavailableError]


def call(name: str, arguments: Any = None, **kwargs: Any) -> ModelReply:
    """A reply containing one native tool call. Pass `arguments` to send a raw (e.g. string) value."""
    return ModelReply(tool_calls=[RawToolCall(name=name, arguments=kwargs if arguments is None else arguments)])


def calls(*pairs: tuple[str, dict[str, Any]]) -> ModelReply:
    """A reply with several (parallel) tool calls — always rejected by the caller."""
    return ModelReply(tool_calls=[RawToolCall(name=n, arguments=a) for n, a in pairs])


def text(content: str) -> ModelReply:
    return ModelReply(content=content)


class ScriptExhaustedError(ModelUnavailableError):
    pass


class ScriptedModel:
    def __init__(self, steps: list[Step] | None = None, *, name: str = "scripted") -> None:
        self.name = name
        self._steps: list[Step] = list(steps or [])
        self.requests: list[ChatRequest] = []

    def extend(self, steps: list[Step]) -> None:
        self._steps.extend(steps)

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def chat(self, request: ChatRequest) -> ModelReply:
        self.requests.append(request)
        if not self._steps:
            raise ScriptExhaustedError(f"script exhausted after {len(self.requests) - 1} replies")
        step = self._steps.pop(0)
        if isinstance(step, ModelReply):
            return step
        if isinstance(step, type):  # an exception class: simulate a runtime failure
            raise step("scripted model failure")
        return step(request)
