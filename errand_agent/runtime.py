"""Wire the runtime components from settings (used by the worker process now, the CLI/API later).

One PostgreSQL database is the only shared state, so the agent, any number of workers and the
in-process mocks can run in separate processes. Memory is optional at runtime: if the local
embedder isn't available it is reported, and the agent says saved preferences are unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from errand_agent.claims import ClaimManager
from errand_agent.clock import Clock, SystemClock
from errand_agent.config import Settings
from errand_agent.db import Database
from errand_agent.db.migrate import pending
from errand_agent.errors import ErrandError
from errand_agent.memory.factory import MemoryConfigError, build_memory
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.backend import MockBackend
from errand_agent.tools.client import ToolClient
from errand_agent.workers.jobs import JobWorker
from errand_agent.workers.outbox import Outbox
from errand_agent.workers.watcher import CancellationWatcher
from errand_agent.workflow.approval import ApprovalGate
from errand_agent.workflow.budget import RunLimits, RunManager
from errand_agent.workflow.executor import OperationExecutor


class SchemaNotCurrentError(ErrandError):
    user_message = "The app's database needs updating before it can run."


@dataclass
class Runtime:
    db: Database
    clock: Clock
    backend: MockBackend
    tools: ToolClient
    gate: ApprovalGate
    executor: OperationExecutor
    claims: ClaimManager
    runs: RunManager
    memory: MemoryStore | None
    memory_note: str
    watcher: CancellationWatcher
    outbox: Outbox

    def worker(self, holder: str) -> JobWorker:
        return JobWorker(
            self.db, self.clock, self.executor, holder=holder, claims=self.claims, runs=self.runs, events=self.watcher
        )

    def close(self) -> None:
        self.db.close()


def build_runtime(settings: Settings, *, clock: Clock | None = None, dbname: str | None = None) -> Runtime:
    clock = clock or SystemClock()
    missing = pending(settings.conninfo(dbname=dbname))
    if missing:
        raise SchemaNotCurrentError(
            f"database is missing migrations {', '.join(missing)}; run: python -m errand_agent.db.migrate"
        )
    db = Database(settings.conninfo(dbname=dbname))
    backend = MockBackend(db, clock, seed=settings.mock_seed)
    tools = ToolClient(backend, db, clock)
    gate = ApprovalGate(db, clock)
    limits = RunLimits()
    claims = ClaimManager(db, clock, lease_seconds=limits.active_seconds + 30)
    runs = RunManager(db, clock, limits)
    memory: MemoryStore | None
    try:
        memory = build_memory(db, clock, settings)
        note = f"on ({memory.embedder.model})"
    except MemoryConfigError as exc:
        memory, note = None, f"off: {exc}"
    watcher = CancellationWatcher(db, clock, tools, gate, claims=claims, runs=runs, memory=memory)
    return Runtime(
        db=db,
        clock=clock,
        backend=backend,
        tools=tools,
        gate=gate,
        executor=OperationExecutor(db, clock, tools),
        claims=claims,
        runs=runs,
        memory=memory,
        memory_note=note,
        watcher=watcher,
        outbox=Outbox(db, clock, gate),
    )
