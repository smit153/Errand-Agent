"""The background loop: ingest provider events, then run due jobs. Stateless; run one or many."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from errand_agent.workers.jobs import JobResult, JobWorker
from errand_agent.workers.watcher import CancellationWatcher


class Tick(BaseModel):
    model_config = ConfigDict(frozen=True)

    ingested: int
    jobs: list[JobResult]


class Runner:
    def __init__(self, watcher: CancellationWatcher, worker: JobWorker, *, batch: int = 10) -> None:
        self._watcher = watcher
        self._worker = worker
        self._batch = batch

    def run_once(self) -> Tick:
        ingested = self._watcher.ingest()
        return Tick(ingested=ingested, jobs=self._worker.run_due(limit=self._batch))

    def drain(self, *, max_ticks: int = 20) -> list[Tick]:
        """Run until a tick does nothing (bounded), e.g. for a demo or a test."""
        ticks: list[Tick] = []
        for _ in range(max_ticks):
            tick = self.run_once()
            ticks.append(tick)
            if tick.ingested == 0 and not tick.jobs:
                break
        return ticks
