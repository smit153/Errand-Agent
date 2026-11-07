"""Build the runtime memory store with the local Ollama embedder (DECISIONS §18)."""

from __future__ import annotations

from errand_agent.clock import Clock
from errand_agent.config import Settings
from errand_agent.db import Database
from errand_agent.errors import ErrandError
from errand_agent.memory.embedder import EmbeddingUnavailableError
from errand_agent.memory.ollama import connect
from errand_agent.memory.store import MemoryStore


class MemoryConfigError(ErrandError):
    user_message = "Saved preferences aren't available right now, so I'm not using any."


def build_memory(db: Database, clock: Clock, settings: Settings) -> MemoryStore:
    """Connects to the installed, pinned embedding model; fails clearly if it is missing."""
    try:
        embedder = connect(settings.embed_url, settings.embed_model, pinned_digest=settings.embed_digest)
    except EmbeddingUnavailableError as exc:
        raise MemoryConfigError(str(exc)) from exc
    return MemoryStore(db, clock, embedder)
