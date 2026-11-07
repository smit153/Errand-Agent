"""Local Ollama chat adapter using native tools (`tools` in, `message.tool_calls` out).

Local only: the base URL must be loopback or a bare local service name (e.g. the Compose service
`ollama`), and cloud model tags are refused, so there is no cloud fallback at runtime.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from errand_agent.llm.types import ChatRequest, ModelReply, ModelUnavailableError, RawToolCall

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class LocalOnlyError(ValueError):
    pass


def check_local(base_url: str, model: str) -> None:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if parsed.scheme != "http":
        raise LocalOnlyError(f"Ollama URL must be plain http on the local machine, got {base_url!r}")
    if host not in _LOOPBACK and ("." in host or ":" in host or not host):
        raise LocalOnlyError(f"Ollama host {host!r} is not loopback or a local service name")
    if "cloud" in model.lower():
        raise LocalOnlyError(f"cloud model tag {model!r} is not allowed")


class OllamaChatModel:
    def __init__(self, base_url: str, model: str, *, think: bool = False) -> None:
        check_local(base_url, model)
        self.name = model
        self._url = base_url.rstrip("/") + "/api/chat"
        self._think = think

    def chat(self, request: ChatRequest) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "think": self._think,
            "options": request.options.model_dump(),
        }
        if request.tools:
            body["tools"] = request.tools
        req = urllib.request.Request(  # noqa: S310 - URL validated as local http in __init__
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout_s) as resp:  # noqa: S310 - checked local URL
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ModelUnavailableError(f"Ollama request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ModelUnavailableError(f"Ollama returned non-JSON: {exc}") from exc
        if not isinstance(data, dict) or "message" not in data:
            raise ModelUnavailableError(f"unexpected Ollama response: {str(data)[:200]}")
        message = data.get("message") or {}
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                calls.append(RawToolCall(name=fn["name"], arguments=fn.get("arguments")))
            else:
                calls.append(RawToolCall(name="<malformed>", arguments=tc))
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=calls,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            model=data.get("model"),
        )
