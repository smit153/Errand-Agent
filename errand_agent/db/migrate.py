"""Forward-only SQL migrations with checksums.

Files live in `errand_agent/db/migrations/NNNN_name.sql` and are applied in order, each in its
own transaction, under a global advisory lock so concurrent starters cannot race.
An already-applied migration whose checksum changed is a hard error, never silently re-run.
"""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from pathlib import Path

import psycopg

from errand_agent.config import load_settings
from errand_agent.errors import MigrationError

_MIGRATION_LOCK_KEY = 0x45_52_52_41_4E_44  # "ERRAND"; distinct from session ids (bigserial)
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _migration_files() -> list[tuple[int, str, str]]:
    root = Path(str(resources.files("errand_agent.db") / "migrations"))
    found: list[tuple[int, str, str]] = []
    for path in sorted(root.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _NAME.match(path.name)
        if match is None:
            raise MigrationError(f"badly named migration file: {path.name}")
        found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 0001: {versions}")
    return found


def migrate(conninfo: str) -> list[str]:
    """Apply pending migrations. Returns the names applied in this call."""
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s::bigint)", (_MIGRATION_LOCK_KEY,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
            done = {int(r[0]): (str(r[1]), str(r[2])) for r in rows}
            for version, name, sql in _migration_files():
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in done:
                    if done[version][1] != checksum:
                        raise MigrationError(f"applied migration {name} was modified after apply")
                    continue
                with conn.transaction():
                    conn.execute(sql.encode("utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, name, checksum),
                    )
                applied_now.append(name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (_MIGRATION_LOCK_KEY,))
    return applied_now


def main() -> None:
    names = migrate(load_settings().conninfo())
    print("applied:", ", ".join(names) if names else "nothing (up to date)")


if __name__ == "__main__":
    main()
