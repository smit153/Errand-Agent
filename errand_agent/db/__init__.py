"""PostgreSQL access: pooled connections, short transactions, per-session advisory locks.

Rules enforced here:
- every transaction sets a bounded lock_timeout and statement_timeout;
- session-scoped work takes `pg_advisory_xact_lock(session_id)` first (global lock order:
  session advisory lock, then that session's rows);
- no transaction is ever held across a model or tool call (callers open one per step).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from errand_agent.errors import SessionBusyError

Conn = psycopg.Connection[DictRow]


class Database:
    def __init__(
        self,
        conninfo: str,
        *,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 5000,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self.pool: ConnectionPool[Conn] = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            timeout=10.0,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def txn(self, *, lock_timeout_ms: int | None = None) -> Iterator[Conn]:
        """A short transaction with bounded lock/statement timeouts. Commits on success."""
        with self.pool.connection() as conn, conn.transaction():
            self._set_timeouts(conn, lock_timeout_ms)
            yield conn

    @contextmanager
    def session_txn(self, session_id: int, *, lock_timeout_ms: int | None = None) -> Iterator[Conn]:
        """A short transaction holding the session's transaction-scoped advisory lock.

        Raises SessionBusyError if the lock cannot be taken within the lock timeout.
        """
        with self.pool.connection() as conn, conn.transaction():
            self._set_timeouts(conn, lock_timeout_ms)
            lock_session(conn, session_id)
            yield conn

    def _set_timeouts(self, conn: Conn, lock_timeout_ms: int | None) -> None:
        lock_ms = self._lock_timeout_ms if lock_timeout_ms is None else lock_timeout_ms
        conn.execute(
            "SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
            (f"{lock_ms}ms", f"{self._statement_timeout_ms}ms"),
        )


def lock_session(conn: Conn, session_id: int) -> None:
    """Take the per-session transaction advisory lock, translating timeouts."""
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (session_id,))
    except pg_errors.LockNotAvailable as exc:
        raise SessionBusyError(f"advisory lock timeout for session {session_id}") from exc


def fetch_one(conn: Conn, query: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> DictRow | None:
    return conn.execute(query, params).fetchone()


def fetch_all(conn: Conn, query: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[DictRow]:
    return conn.execute(query, params).fetchall()
