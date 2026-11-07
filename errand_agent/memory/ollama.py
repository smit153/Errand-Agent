"""Local Ollama embedder (DECISIONS §§10, 18): `nomic-embed-text:v1.5`, 768 dimensions.

Loopback only, `truncate: false` (over-long input is an error, never silently cut), at most
10 seconds per request. Every response is validated (count, dimension, finite, non-zero) before
use. `model` includes the installed model digest, so facts embedded by a different model build
are never compared with today's query vectors; they are re-embedded instead.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from errand_agent.localnet import check_local
from errand_agent.memory.embedder import DIMENSIONS, EmbeddingUnavailableError, Kind, prefixed, validate_vector

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text:v1.5"
TIMEOUT_S = 10.0


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        *,
        digest: str,
        timeout_s: float = TIMEOUT_S,
        dimensions: int = DIMENSIONS,
    ) -> None:
        check_local(base_url, what="Ollama")
        if not model or not digest:
            raise ValueError("an Ollama model name and its installed digest are required")
        self._url = base_url.rstrip("/")
        self._name = model
        self._digest = digest
        self._timeout = min(timeout_s, TIMEOUT_S)
        self._dims = dimensions

    @property
    def model(self) -> str:
        return f"ollama:{self._name}@{self._digest[:12]}"

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self._name, "input": [prefixed(t, kind) for t in texts], "truncate": False}
        data = _post(f"{self._url}/api/embed", body, self._timeout)
        raw = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingUnavailableError("Ollama returned the wrong number of embeddings")
        return [validate_vector(v, self._dims) for v in raw]


def installed_digest(base_url: str, model: str, *, timeout_s: float = TIMEOUT_S) -> str:
    """The digest of the locally installed model (from /api/tags); fails if it isn't installed."""
    check_local(base_url, what="Ollama")
    data = _get(f"{base_url.rstrip('/')}/api/tags", timeout_s)
    models = data.get("models") if isinstance(data, dict) else None
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and m.get("name") == model and isinstance(m.get("digest"), str):
            return str(m["digest"])
    raise EmbeddingUnavailableError(f"Ollama model {model!r} is not installed (run: ollama pull {model})")


def connect(base_url: str, model: str, *, pinned_digest: str | None) -> OllamaEmbedder:
    """An embedder for the installed model; refuses a digest that differs from the pinned one."""
    digest = installed_digest(base_url, model)
    if pinned_digest is not None and digest != pinned_digest:
        raise EmbeddingUnavailableError(
            f"installed {model} digest {digest[:12]} differs from the pinned {pinned_digest[:12]}"
        )
    return OllamaEmbedder(base_url, model, digest=digest)


def _post(url: str, body: dict[str, Any], timeout_s: float) -> Any:
    req = urllib.request.Request(  # noqa: S310 - URL checked as local http
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
    )
    return _send(req, timeout_s)


def _get(url: str, timeout_s: float) -> Any:
    return _send(urllib.request.Request(url, method="GET"), timeout_s)  # noqa: S310 - URL checked as local http


def _send(req: urllib.request.Request, timeout_s: float) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - URL checked as local http
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise EmbeddingUnavailableError(f"Ollama HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise EmbeddingUnavailableError(f"Ollama unreachable: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise EmbeddingUnavailableError("Ollama returned non-JSON") from exc
