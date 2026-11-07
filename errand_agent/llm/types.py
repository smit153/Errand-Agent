"""Model I/O types shaped after Ollama's native `/api/chat` tools interface.

Everything a model returns is untrusted data: callers validate it before use and never execute
or evaluate it.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ModelOptions(BaseModel):
    """Fixed decoding settings (G8). Seeded, but bitwise determinism is not promised."""

    model_config = ConfigDict(frozen=True)

    temperature: float = 0.0
    seed: int = 42
    num_predict: int = Field(default=768, ge=1)
    num_ctx: int = Field(default=8192, ge=512)


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[Message]
    tools: list[dict[str, Any]]
    options: ModelOptions
    timeout_s: float = Field(gt=0)


class RawToolCall(BaseModel):
    """A tool call exactly as the model emitted it; `arguments` may be a dict or a string."""

    model_config = ConfigDict(frozen=True)

    name: str
    arguments: Any


class ModelReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str = ""
    tool_calls: list[RawToolCall] = Field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None


class ModelUnavailableError(Exception):
    """Connection failure, timeout or runtime error. Counts as a failed physical attempt."""


class ChatModel(Protocol):
    name: str

    def chat(self, request: ChatRequest) -> ModelReply: ...
