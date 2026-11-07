"""Record real embeddings for the labeled pairs into tests/fixtures/nomic_vectors.json (E43).

Run in the dev container with the Ollama service up and the model pulled:
    ERRAND_EMBED_URL=http://ollama:11434 scripts/dev.sh python scripts/record_embedding_fixtures.py

The file records the model name, installed digest and every vector, so offline tests can run the
real ranking/threshold code on real-model geometry without Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from errand_agent.config import load_settings
from errand_agent.memory.ollama import connect

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ROOT / "tests" / "fixtures" / "embedding_pairs.json"
OUT = ROOT / "tests" / "fixtures" / "nomic_vectors.json"


def main() -> int:
    settings = load_settings()
    emb = connect(settings.embed_url, settings.embed_model, pinned_digest=settings.embed_digest)
    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))
    queries = list(pairs["queries"].values())
    documents = [d for docs in pairs["documents"].values() for d in docs]
    vectors: dict[str, list[float]] = {}
    for q, v in zip(queries, emb.embed(queries, kind="query"), strict=True):
        vectors["search_query: " + q] = [round(x, 7) for x in v]
    for d, v in zip(documents, emb.embed(documents, kind="document"), strict=True):
        vectors["search_document: " + d] = [round(x, 7) for x in v]
    OUT.write_text(
        json.dumps({"model": emb.model, "name": settings.embed_model, "vectors": vectors}, indent=0) + "\n",
        encoding="utf-8",
    )
    print(f"recorded {len(vectors)} vectors from {emb.model} into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
