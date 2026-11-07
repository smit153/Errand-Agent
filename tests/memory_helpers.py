"""Test-only embedders and memory fixtures."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from errand_agent.db import Database
from errand_agent.memory.embedder import DIMENSIONS, EmbeddingUnavailableError, Kind, prefixed, validate_vector


class HashEmbedder:
    """Deterministic pseudo-random unit vectors per prefixed text; unrelated texts score near 0.

    `overrides` pins exact vectors for texts whose similarity a test controls.
    """

    def __init__(self, model: str = "test-hash-768") -> None:
        self._model = model
        self.available = True
        self.overrides: dict[str, list[float]] = {}
        self.calls = 0

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        self.calls += 1
        if not self.available:
            raise EmbeddingUnavailableError("test embedder switched off")
        out = []
        for text in texts:
            key = prefixed(text, kind)
            if key in self.overrides:
                out.append(validate_vector(self.overrides[key]))
                continue
            seed = hashlib.sha256(key.encode()).digest()
            raw: list[float] = []
            counter = 0
            while len(raw) < DIMENSIONS:
                block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
                raw.extend((b - 127.5) / 127.5 for b in block)
                counter += 1
            out.append(validate_vector(raw[:DIMENSIONS]))
        return out

    def pin(self, text: str, kind: Kind, vector: list[float]) -> None:
        self.overrides[prefixed(text, kind)] = vector

    def align_query(self, text: str) -> None:
        """Make `text` as a query embed exactly like `text` as a document (cosine 1.0)."""
        self.pin(text, "query", self.embed([text], kind="document")[0])


def succeeded_booking(
    db: Database,
    *,
    session_id: int,
    user_id: str,
    provider_id: str,
    provider_name: str,
    service: str,
    slot: datetime,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> None:
    """A completed, user-approved booking operation, as the executor leaves it."""
    from tests.helpers import make_operation

    snapshot = {
        "action": "book",
        "provider_id": provider_id,
        "provider_name": provider_name,
        "service": service,
        "slot": slot.isoformat(),
        "price_inr": 900,
        "hold_id": f"H-{provider_id}-{slot:%m%d%H}",
        "hold_expires_at": (now + timedelta(minutes=10)).isoformat(),
        **(extra or {}),
    }
    op = make_operation(
        db,
        session_id=session_id,
        user_id=user_id,
        action="book",
        snapshot=snapshot,
        key=f"op-{provider_id}-{slot:%m%d%H%M}",
        now=now,
    )
    with db.txn() as conn:
        conn.execute(
            "UPDATE operations SET status = 'succeeded', outcome = %s WHERE id = %s",
            (Jsonb({"booking_id": f"BK-{slot:%m%d%H}"}), op),
        )


_TOPICS: list[tuple[str, ...]] = [
    ("plumb", "tap", "water", "leak", "flush", "sink", "washer", "geyser", "valve", "pressure", "pipe"),
    ("electric", "wiring", "switch", "fan", "mcb", "inverter"),
    ("ac ", "ac:", "cooling", "filter", "gas refill", "split", "drips"),
    ("carpent", "wardrobe", "cabinet", "teak", "bookshelf", "hinge"),
    ("clean", "sofa", "chimney", "glass"),
    ("indiranagar", "koramangala", "area", "whitefield"),
    ("time", "morning", " am", " pm"),
    ("call", "dog", "parking", "upi", "payment"),
]


class TopicEmbedder:
    """Deterministic 'semantic' vectors for offline evals: one axis per topic word list, plus a
    small per-text hash component so different texts never tie exactly by accident."""

    def __init__(self, model: str = "test-topic-768") -> None:
        self._model = model
        self._noise = HashEmbedder(model)
        self.calls = 0
        self.available = True

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str], *, kind: Kind) -> list[list[float]]:
        self.calls += 1
        if not self.available:
            raise EmbeddingUnavailableError("topic embedder switched off")
        out = []
        for text in texts:
            low = f" {text.lower()} "
            noise = self._noise.embed([text], kind="document")[0]
            v = [0.08 * x for x in noise]
            for axis, words in enumerate(_TOPICS):
                v[axis] += sum(low.count(w) for w in words)
            out.append(validate_vector(v))
        return out
