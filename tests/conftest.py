"""Shared fixtures. Tests run against the same PostgreSQL 16 + pgvector image and migrations as runtime.

The test database is dropped and recreated once per pytest session; every test then starts from
truncated tables. Rollback-per-test isolation is not used because concurrency tests need real
commits across several connections.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from errand_agent.clock import FakeClock
from errand_agent.config import Settings, load_settings
from errand_agent.db import Database
from errand_agent.db.migrate import migrate

# Friday 6 November 2026, 08:00 Asia/Kolkata (02:30 UTC).
T0 = datetime(2026, 11, 6, 2, 30, tzinfo=UTC)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def test_conninfo(settings: Settings) -> str:
    name = settings.test_db_name
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name) or name == settings.db_name:
        raise RuntimeError(f"refusing to use {name!r} as a disposable test database")
    with psycopg.connect(settings.conninfo(dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info = settings.conninfo(dbname=name)
    migrate(info)
    return info


def _truncate_all(conninfo: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
            ).fetchall()
        ]
        if tables:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tables)
                )
            )


@pytest.fixture
def db(test_conninfo: str) -> Iterator[Database]:
    _truncate_all(test_conninfo)
    database = Database(test_conninfo, max_size=12)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def user_id(db: Database) -> str:
    with db.txn() as conn:
        conn.execute("INSERT INTO users (id) VALUES ('user-demo')")
    return "user-demo"


def make_session(db: Database, user: str, now: datetime) -> int:
    with db.txn() as conn:
        row = conn.execute(
            "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s) RETURNING id",
            (user, now, now),
        ).fetchone()
    assert row is not None
    return int(row["id"])
