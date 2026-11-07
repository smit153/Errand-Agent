"""Stage 5d: the Ollama embedder against a fake local server (offline; the real check is E43).

Its output is untrusted: wrong counts, sizes or values are refused, failures become
EmbeddingUnavailableError (memory then degrades explicitly), and only loopback URLs are used.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from errand_agent.localnet import LocalOnlyError
from errand_agent.memory.embedder import DIMENSIONS, EmbeddingUnavailableError
from errand_agent.memory.ollama import TIMEOUT_S, OllamaEmbedder, connect, installed_digest

DIGEST = "a" * 64


class _FakeOllama(BaseHTTPRequestHandler):
    bodies: ClassVar[list[dict[str, Any]]] = []
    reply: ClassVar[Any] = None
    status: ClassVar[int] = 200

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOllama.bodies.append(body)
        reply = _FakeOllama.reply
        if reply is None:
            reply = {"embeddings": [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in body["input"]]}
        self._send(reply)

    def do_GET(self) -> None:
        self._send({"models": [{"name": "nomic-embed-text:v1.5", "digest": DIGEST}]})

    def _send(self, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(_FakeOllama.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


@pytest.fixture
def fake_ollama() -> Iterator[str]:
    _FakeOllama.bodies, _FakeOllama.reply, _FakeOllama.status = [], None, 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def test_requests_use_prefixes_and_never_truncate(fake_ollama: str) -> None:
    emb = OllamaEmbedder(fake_ollama, digest=DIGEST)
    vectors = emb.embed(["kitchen tap"], kind="query")
    emb.embed(["Preferred area: Indiranagar"], kind="document")
    assert _FakeOllama.bodies == [
        {"model": "nomic-embed-text:v1.5", "input": ["search_query: kitchen tap"], "truncate": False},
        {
            "model": "nomic-embed-text:v1.5",
            "input": ["search_document: Preferred area: Indiranagar"],
            "truncate": False,
        },
    ]
    assert len(vectors[0]) == DIMENSIONS
    assert emb.model == f"ollama:nomic-embed-text:v1.5@{DIGEST[:12]}"


@pytest.mark.parametrize(
    "reply",
    [
        {"embeddings": []},  # wrong count
        {"embeddings": [[1.0] * 10]},  # wrong dimension
        {"embeddings": [[float("inf")] + [0.0] * (DIMENSIONS - 1)]},
        {"embeddings": [["x"] * DIMENSIONS]},
        {"error": "model not found"},
        ["not", "an", "object"],
    ],
)
def test_untrusted_output_is_refused(fake_ollama: str, reply: Any) -> None:
    _FakeOllama.reply = reply
    with pytest.raises(EmbeddingUnavailableError):
        OllamaEmbedder(fake_ollama, digest=DIGEST).embed(["x"], kind="query")


def test_http_errors_and_unreachable_server_are_explicit(fake_ollama: str) -> None:
    _FakeOllama.status = 500
    with pytest.raises(EmbeddingUnavailableError, match="HTTP 500"):
        OllamaEmbedder(fake_ollama, digest=DIGEST).embed(["x"], kind="query")
    with pytest.raises(EmbeddingUnavailableError, match="unreachable"):
        OllamaEmbedder("http://127.0.0.1:9", digest=DIGEST).embed(["x"], kind="query")


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:11434", "http://ollama.example.com:11434", "http://10.0.0.5:11434", "http://u:p@127.0.0.1:1"],
)
def test_only_local_plain_http(url: str) -> None:
    with pytest.raises(LocalOnlyError):
        OllamaEmbedder(url, digest=DIGEST)


def test_timeout_never_exceeds_ten_seconds() -> None:
    emb = OllamaEmbedder("http://127.0.0.1:1", digest=DIGEST, timeout_s=120)
    assert emb._timeout == TIMEOUT_S == 10.0


def test_installed_digest_and_pinning(fake_ollama: str) -> None:
    assert installed_digest(fake_ollama, "nomic-embed-text:v1.5") == DIGEST
    with pytest.raises(EmbeddingUnavailableError, match="not installed"):
        installed_digest(fake_ollama, "other-model")
    assert connect(fake_ollama, "nomic-embed-text:v1.5", pinned_digest=DIGEST).model.endswith(DIGEST[:12])
    with pytest.raises(EmbeddingUnavailableError, match="differs from the pinned"):
        connect(fake_ollama, "nomic-embed-text:v1.5", pinned_digest="b" * 64)
