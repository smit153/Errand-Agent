"""Embedding contract (DECISIONS §§10, 18).

Production uses local Ollama `nomic-embed-text:v1.5` (stage 5d). Ordinary evals use
`FixtureEmbedder`, which returns checked-in vectors and fails clearly for any text it doesn't
know, so a test can never silently embed something unexpected. Every embedder output is
validated (dimension, finite values) and unit-normalized before it is stored or compared.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol

from errand_agent.errors import BudgetExhaustedError

DIMENSIONS = 768
Kind = Literal["query", "document"]
PREFIX: dict[Kind, str] = {"query": "search_query: ", "document": "search_document: "}


class EmbeddingUnavailableError(Exception):
    """The embedder failed, timed out or returned an invalid vector. Callers degrade explicitly."""


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]: ...


def prefixed(text: str, kind: Kind) -> str:
    return PREFIX[kind] + text


def validate_vector(raw: object, dimensions: int = DIMENSIONS) -> list[float]:
    """Untrusted embedder output → a unit vector of the right size, or EmbeddingUnavailableError."""
    if not isinstance(raw, list) or len(raw) != dimensions:
        size = len(raw) if isinstance(raw, list) else type(raw).__name__
        raise EmbeddingUnavailableError(f"embedding has wrong shape: {size}, expected {dimensions}")
    values: list[float] = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
            raise EmbeddingUnavailableError("embedding contains a non-finite or non-numeric value")
        values.append(float(x))
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise EmbeddingUnavailableError("embedding is the zero vector")
    return [v / norm for v in values]


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text input (passed as a bound parameter and cast with ::vector, never interpolated)."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


class FixtureEmbedder:
    """Checked-in vectors keyed by the exact prefixed text. Unknown text fails; nothing is invented."""

    def __init__(self, vectors: Mapping[str, Sequence[float]], *, model: str = "fixture-nomic-768") -> None:
        self._vectors = {k: list(v) for k, v in vectors.items()}
        self._model = model
        self.available = True
        self.requests: list[tuple[Kind, list[str]]] = []

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        self.requests.append((kind, list(texts)))
        if not self.available:
            raise EmbeddingUnavailableError("fixture embedder switched off")
        out: list[list[float]] = []
        for text in texts:
            key = prefixed(text, kind)
            if key not in self._vectors:
                raise EmbeddingUnavailableError(f"no fixture vector for {key!r}")
            out.append(validate_vector(self._vectors[key]))
        return out

    def add(self, text: str, kind: Kind, vector: Sequence[float]) -> None:
        self._vectors[prefixed(text, kind)] = list(vector)


def basis(*weights: tuple[int, float]) -> list[float]:
    """A test vector with the given (axis, weight) components; handy for controlled cosine values."""
    v = [0.0] * DIMENSIONS
    for axis, w in weights:
        v[axis] = w
    return v


class BudgetedEmbedder:
    """Per-run wrapper: at most `limit` embedding requests per run (DECISIONS §2); repeated
    query texts reuse the vector already computed in this run."""

    def __init__(self, base: Embedder, *, limit: int = 4, charge: Callable[[], None] | None = None) -> None:
        """`charge` records each request against the run (and may refuse it by raising)."""
        self._base = base
        self._limit = limit
        self._charge = charge
        self.requests = 0
        self._queries: dict[str, list[float]] = {}

    @property
    def model(self) -> str:
        return self._base.model

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        if kind == "query" and all(t in self._queries for t in texts):
            return [self._queries[t] for t in texts]
        if self.requests >= self._limit:
            raise EmbeddingUnavailableError(f"per-run embedding limit of {self._limit} requests reached")
        if self._charge is not None:
            try:
                self._charge()
            except BudgetExhaustedError as exc:
                raise EmbeddingUnavailableError(f"run limit reached: {exc.limit}") from exc
        self.requests += 1
        vectors = [validate_vector(v) for v in self._base.embed(texts, kind=kind)]
        if len(vectors) != len(texts):
            raise EmbeddingUnavailableError(f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
        if kind == "query":
            self._queries.update(zip(texts, vectors, strict=True))
        return vectors
