"""Stage 1 gate (E43 skeleton, E32 primitives): real Postgres, migrations, locking, constraints."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import pytest
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from errand_agent.clock import FakeClock
from errand_agent.db import Conn, Database
from errand_agent.db.migrate import migrate
from errand_agent.errors import SessionBusyError
from tests.conftest import make_session


def test_server_is_postgres16_with_pgvector(db: Database) -> None:
    with db.txn() as conn:
        version = conn.execute("SHOW server_version_num").fetchone()
        ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert version is not None
    assert int(version["server_version_num"]) // 10000 == 16
    assert ext is not None
    assert ext["extversion"] == "0.8.1"


def test_migrations_are_recorded_and_idempotent(db: Database, test_conninfo: str) -> None:
    assert migrate(test_conninfo) == []
    with db.txn() as conn:
        rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    assert rows[0]["name"] == "0001_init.sql"


def test_transaction_rolls_back_on_error(db: Database, user_id: str, clock: FakeClock) -> None:
    def insert_then_fail() -> None:
        with db.txn() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s)",
                (user_id, clock.now(), clock.now()),
            )
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        insert_then_fail()
    with db.txn() as conn:
        count = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()
    assert count is not None
    assert count["n"] == 0


def _hold_session_lock(db: Database, session_id: int, held: threading.Event, release: threading.Event) -> None:
    with db.session_txn(session_id):
        held.set()
        release.wait(5)


def test_session_lock_times_out_with_busy_error(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_session_lock, args=(db, sid, held, release))
    t.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        with pytest.raises(SessionBusyError), db.session_txn(sid, lock_timeout_ms=150):
            pass
        assert time.monotonic() - started < 2.0  # bounded wait, never a hang
    finally:
        release.set()
        t.join(5)


def test_session_lock_serializes_then_proceeds(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    order: list[str] = []
    held, release = threading.Event(), threading.Event()

    def first() -> None:
        with db.session_txn(sid):
            order.append("first-in")
            held.set()
            release.wait(5)
            order.append("first-out")

    t = threading.Thread(target=first)
    t.start()
    assert held.wait(5)
    threading.Timer(0.2, release.set).start()
    with db.session_txn(sid, lock_timeout_ms=2000):
        order.append("second-in")
    t.join(5)
    assert order == ["first-in", "first-out", "second-in"]


def test_unrelated_sessions_do_not_block(db: Database, user_id: str, clock: FakeClock) -> None:
    s1 = make_session(db, user_id, clock.now())
    s2 = make_session(db, user_id, clock.now())
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_session_lock, args=(db, s1, held, release))
    t.start()
    try:
        assert held.wait(5)
        with db.session_txn(s2, lock_timeout_ms=150) as conn:
            conn.execute("UPDATE sessions SET revision = revision + 1 WHERE id = %s", (s2,))
    finally:
        release.set()
        t.join(5)


# ------------------------------------------------------------------ constraint backstops


def _proposal(conn: Conn, sid: int, rev: int, status: str, clock: FakeClock) -> uuid.UUID:
    pid = uuid.uuid4()
    conn.execute(
        "INSERT INTO proposals (id, session_id, revision, action, snapshot, snapshot_hash, token, status,"
        " created_at) VALUES (%s, %s, %s, 'book', %s, 'h', %s, %s, %s)",
        (pid, sid, rev, Jsonb({}), f"tok-{pid.hex[:8]}", status, clock.now()),
    )
    return pid


@pytest.mark.invariant
def test_only_one_pending_proposal_per_session(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    with db.txn() as conn:
        _proposal(conn, sid, 1, "pending", clock)
    with pytest.raises(pg_errors.UniqueViolation), db.txn() as conn:
        _proposal(conn, sid, 2, "pending", clock)


@pytest.mark.invariant
def test_approval_and_operation_are_unique_per_proposal(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    now = clock.now()
    with db.txn() as conn:
        pid = _proposal(conn, sid, 1, "approved", clock)
        rid = uuid.uuid4()
        conn.execute(
            "INSERT INTO display_receipts (id, session_id, proposal_id, displayed_at) VALUES (%s, %s, %s, %s)",
            (rid, sid, pid, now),
        )
        aid = uuid.uuid4()
        conn.execute(
            "INSERT INTO approvals (id, proposal_id, session_id, receipt_id, form, approved_at)"
            " VALUES (%s, %s, %s, %s, 'yes', %s)",
            (aid, pid, sid, rid, now),
        )
        conn.execute(
            "INSERT INTO operations (id, session_id, user_id, proposal_id, approval_id, action, snapshot,"
            " idempotency_key, status, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, 'book', %s, 'k1', 'approved', %s, %s)",
            (uuid.uuid4(), sid, user_id, pid, aid, Jsonb({}), now, now),
        )
    with pytest.raises(pg_errors.UniqueViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO approvals (id, proposal_id, session_id, receipt_id, form, approved_at)"
            " VALUES (%s, %s, %s, %s, 'token', %s)",
            (uuid.uuid4(), pid, sid, rid, now),
        )
    with pytest.raises(pg_errors.UniqueViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO operations (id, session_id, user_id, proposal_id, approval_id, action, snapshot,"
            " idempotency_key, status, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, 'book', %s, 'k2', 'approved', %s, %s)",
            (uuid.uuid4(), sid, user_id, pid, aid, Jsonb({}), now, now),
        )
    # Lifetime dispatch cap is a hard constraint, not just a code check.
    with pytest.raises(pg_errors.CheckViolation), db.txn() as conn:
        conn.execute("UPDATE operations SET dispatch_count = 4")


@pytest.mark.invariant
def test_inferred_memory_cannot_be_active_unconfirmed(db: Database, user_id: str, clock: FakeClock) -> None:
    now = clock.now()
    with pytest.raises(pg_errors.CheckViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO memories (id, user_id, display_no, field, value, origin, status, confirmed, provenance,"
            " created_at, updated_at) VALUES (%s, %s, 1, 'earliest_time', %s, 'inferred', 'active', false,"
            " %s, %s, %s)",
            (uuid.uuid4(), user_id, Jsonb({"kind": "earliest_time", "earliest": "10:00:00"}), Jsonb({}), now, now),
        )


@pytest.mark.invariant
def test_provider_slot_cannot_be_double_booked(db: Database, clock: FakeClock) -> None:
    now = clock.now()
    slot = now + timedelta(days=1)
    with db.txn() as conn:
        conn.execute(
            "INSERT INTO mock_providers (id, service, area, name, base_price_inr, rating, attributes, schedule,"
            " description) VALUES ('p1', 'plumber', 'indiranagar', 'P', 900, 4.5, %s, %s, 'd')",
            (Jsonb({}), Jsonb({})),
        )
        conn.execute(
            "INSERT INTO mock_bookings (id, user_id, provider_id, service, slot, price_inr, status, created_at,"
            " updated_at) VALUES ('b1', 'u', 'p1', 'plumber', %s, 900, 'confirmed', %s, %s)",
            (slot, now, now),
        )
    with pytest.raises(pg_errors.UniqueViolation), db.txn() as conn:
        conn.execute(
            "INSERT INTO mock_bookings (id, user_id, provider_id, service, slot, price_inr, status, created_at,"
            " updated_at) VALUES ('b2', 'u', 'p1', 'plumber', %s, 900, 'confirmed', %s, %s)",
            (slot, now, now),
        )
