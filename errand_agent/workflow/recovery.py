"""Restart recovery: fence the previous worker, then reconcile before any retry.

A recovering worker acquires the session claim (an expired lease is taken over and the previous
holder's generation is fenced), starts a fresh run, and resumes every unresolved operation.
`OperationExecutor.resume` treats any reserved-but-unrecorded attempt as *unknown* and checks the
source of truth before deciding to retry, so a crash at any point cannot cause a duplicate effect.
"""

from __future__ import annotations

from uuid import UUID

from errand_agent import trace
from errand_agent.claims import ClaimManager
from errand_agent.clock import Clock
from errand_agent.db import Database, fetch_all
from errand_agent.errors import StaleClaimError
from errand_agent.workflow.budget import RunManager
from errand_agent.workflow.executor import ExecutionOutcome, OperationExecutor

RECOVERABLE = ("executing", "verifying", "pending_verification", "retry_wait")


class RecoveryService:
    def __init__(
        self, db: Database, clock: Clock, executor: OperationExecutor, claims: ClaimManager, runs: RunManager
    ) -> None:
        self._db = db
        self._clock = clock
        self._executor = executor
        self._claims = claims
        self._runs = runs

    def unresolved_operations(self, session_id: int) -> list[UUID]:
        with self._db.txn() as conn:
            rows = fetch_all(
                conn,
                "SELECT id FROM operations WHERE session_id = %s AND status = ANY(%s) ORDER BY created_at, id",
                (session_id, list(RECOVERABLE)),
            )
        return [r["id"] for r in rows]

    def recover_session(self, session_id: int, holder: str) -> list[ExecutionOutcome]:
        """Raises SessionBusyError while another worker's lease is live."""
        claim = self._claims.acquire(session_id, holder)
        run = self._runs.start(claim)
        exit_reason = "recovered"
        try:
            ops = self.unresolved_operations(session_id)
            with self._db.session_txn(session_id) as conn:
                trace.record(
                    conn,
                    "recovery.started",
                    {"holder": holder, "generation": claim.generation, "operations": [str(o) for o in ops]},
                    at=self._clock.now(),
                    run_id=run.id,
                    session_id=session_id,
                )
            return [self._executor.resume(run, op) for op in ops]
        except StaleClaimError:
            exit_reason = "fenced"
            raise
        except Exception as exc:
            exit_reason = f"error: {type(exc).__name__}"
            raise
        finally:
            self._runs.end(run, exit_reason)
            self._claims.release(claim)
