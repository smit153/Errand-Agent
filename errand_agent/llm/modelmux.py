"""ModelMux chat adapter: OpenAI-compatible Chat Completions with `tools` (DECISIONS §17).

ModelMux runs on this machine and fronts Claude Code or Codex; the models themselves run on the
providers' servers. The base URL must be loopback or a bare local service name. The gateway key
comes from configuration, is sent only as a bearer token and never appears in errors or traces.

Action calls use `tool_choice: "required"` and `parallel_tool_calls: false`. ModelMux simulates
tool calls; the caller still validates every one. `temperature`, `seed` and `max_tokens` are sent
but ignored by ModelMux, which lists them in `X-ModelMux-Ignored-Params`; that list is returned so
it can be traced.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Literal

from pydantic import SecretStr

from errand_agent.llm.types import ChatRequest, ModelReply, ModelUnavailableError, RawToolCall
from errand_agent.localnet import LocalOnlyError
from errand_agent.localnet import check_local as _check_local

Provider = Literal["claude", "codex"]
DEFAULT_URLS: dict[str, str] = {"claude": "http://127.0.0.1:8101/v1", "codex": "http://127.0.0.1:8102/v1"}
__all__ = ["DEFAULT_URLS", "LocalOnlyError", "ModelMuxChatModel", "check_local", "list_models"]


def check_local(base_url: str) -> None:
    """The gateway must be reached over plain http on this machine (loopback or a local service name)."""
    _check_local(base_url, what="ModelMux")


def _decode_arguments(raw: Any) -> Any:
    """OpenAI sends arguments as a JSON string; decode valid JSON, pass anything else through to validation."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


class ModelMuxChatModel:
    def __init__(self, base_url: str, model: str, api_key: SecretStr, *, provider: Provider) -> None:
        check_local(base_url)
        if not model:
            raise ValueError("a ModelMux model id is required")
        self.name = f"modelmux:{provider}:{model}"
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key

    def chat(self, request: ChatRequest) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            # Sent for completeness; ModelMux reports them as ignored (traced, not claimed).
            "temperature": request.options.temperature,
            "seed": request.options.seed,
            "max_tokens": request.options.num_predict,
        }
        if request.tools:
            body.update(tools=request.tools, tool_choice="required", parallel_tool_calls=False)
        req = urllib.request.Request(  # noqa: S310 - URL validated as local http in __init__
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key.get_secret_value()}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout_s) as resp:  # noqa: S310 - checked local URL
                data = json.loads(resp.read())
                ignored = [
                    p.strip() for p in (resp.headers.get("X-ModelMux-Ignored-Params") or "").split(",") if p.strip()
                ]
        except urllib.error.HTTPError as exc:
            raise ModelUnavailableError(f"ModelMux HTTP {exc.code}: {_error_code(exc)}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ModelUnavailableError(f"ModelMux unreachable: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ModelUnavailableError("ModelMux returned non-JSON") from exc
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailableError("unexpected ModelMux response shape") from exc
        calls: list[RawToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                calls.append(RawToolCall(name=fn["name"], arguments=_decode_arguments(fn.get("arguments"))))
            else:
                calls.append(RawToolCall(name="<malformed>", arguments=None))
        usage = data.get("usage") or {}
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=calls,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            model=data.get("model"),
            ignored_params=ignored,
        )


def list_models(base_url: str, api_key: SecretStr, *, timeout_s: float = 10.0) -> list[str]:
    """Model ids the gateway offers (Codex model ids depend on the account)."""
    check_local(base_url)
    req = urllib.request.Request(  # noqa: S310 - URL validated by check_local
        base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key.get_secret_value()}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - checked local URL
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ModelUnavailableError(f"ModelMux HTTP {exc.code}: {_error_code(exc)}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
        raise ModelUnavailableError(f"ModelMux /models failed: {type(exc).__name__}") from exc
    return [str(m["id"]) for m in data.get("data", []) if isinstance(m, dict) and "id" in m]


def _error_code(exc: urllib.error.HTTPError) -> str:
    """OpenAI-shaped error code only (never the request, which carries the key in a header)."""
    try:
        payload = json.loads(exc.read())
        return str(payload["error"].get("code") or payload["error"].get("type") or "error")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return "error"
