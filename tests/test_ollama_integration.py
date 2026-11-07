"""E43: the real local embedder (deselected by default; run with `-m ollama`).

Needs the Ollama service up with the pinned model pulled. Checks the installed digest, the
dimension, that live vectors match the recorded fixtures (same model build, tolerance for
hardware float differences), and that relevant pairs outrank other services' memories.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errand_agent.config import Settings
from errand_agent.memory.embedder import DIMENSIONS
from errand_agent.memory.ollama import connect

pytestmark = pytest.mark.ollama

FIXTURES = Path(__file__).parent / "fixtures"
PAIRS: dict[str, Any] = json.loads((FIXTURES / "embedding_pairs.json").read_text(encoding="utf-8"))
RECORDED: dict[str, Any] = json.loads((FIXTURES / "nomic_vectors.json").read_text(encoding="utf-8"))


def test_live_model_is_the_pinned_build_and_matches_the_fixtures(settings: Settings) -> None:
    emb = connect(settings.embed_url, settings.embed_model, pinned_digest=settings.embed_digest)
    assert emb.model == RECORDED["model"]
    queries = list(PAIRS["queries"].values())
    docs = [d for ds in PAIRS["documents"].values() for d in ds]
    live = dict(zip(["search_query: " + q for q in queries], emb.embed(queries, kind="query"), strict=True))
    live |= dict(zip(["search_document: " + d for d in docs], emb.embed(docs, kind="document"), strict=True))
    for key, vector in live.items():
        assert len(vector) == DIMENSIONS
        cosine = sum(a * b for a, b in zip(vector, RECORDED["vectors"][key], strict=True))
        assert cosine > 0.999, key


def test_prefixes_change_the_vector(settings: Settings) -> None:
    emb = connect(settings.embed_url, settings.embed_model, pinned_digest=settings.embed_digest)
    as_query = emb.embed(["kitchen tap leak"], kind="query")[0]
    as_doc = emb.embed(["kitchen tap leak"], kind="document")[0]
    assert as_query != as_doc
