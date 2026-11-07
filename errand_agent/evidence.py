"""Real end-to-end evidence run (PDF §5): one full booking whose `book` call times out after the
booking actually happened, with the real model through ModelMux.

The plumber example is run against a fresh `errand_evidence` database, seeded mocks and an
injected clock; the backend is configured so the booking succeeds but the client sees a timeout.
The agent must force `get_booking_status` before reporting anything. The session's full trace is
exported as JSONL to artifacts/traces/. This is an actual run, not a synthetic trace.

Usage (from the host): scripts/evidence.sh [claude|codex]
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

from errand_agent.clock import FakeClock
from errand_agent.config import Settings, load_settings
from errand_agent.db import Database
from errand_agent.db.migrate import migrate
from errand_agent.llm.factory import build_chat_model
from errand_agent.memory.factory import MemoryConfigError, build_memory
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.backend import MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.trace_export import session_events, write_jsonl
from errand_agent.workflow.agent import Agent

T0 = datetime(2026, 11, 6, 2, 30, tzinfo=UTC)  # Friday 6 Nov 2026, 08:00 Asia/Kolkata
EVIDENCE_DB = "errand_evidence"
TURNS = ["Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.", "Indiranagar", "yes"]


def _fresh_database(settings: Settings) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", EVIDENCE_DB) or settings.db_name == EVIDENCE_DB:
        raise RuntimeError("refusing to use the evidence database name")
    with psycopg.connect(settings.conninfo(dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(EVIDENCE_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(EVIDENCE_DB)))
    info = settings.conninfo(dbname=EVIDENCE_DB)
    migrate(info)
    return info


def main() -> int:
    settings = load_settings()
    model = build_chat_model(settings)
    db = Database(_fresh_database(settings))
    clock = FakeClock(T0)
    backend = MockBackend(db, clock, seed=settings.mock_seed)
    backend.seed(anchor=T0)
    memory: MemoryStore | None
    try:
        memory = build_memory(db, clock, settings)
    except MemoryConfigError:
        memory = None
    agent = Agent(db, clock, ToolClient(backend, db, clock), model, holder="evidence", memory=memory)
    try:
        with db.txn() as conn:
            row = conn.execute(
                "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s) RETURNING id",
                (DEMO_USER, clock.now(), clock.now()),
            ).fetchone()
        if row is None:
            raise RuntimeError("could not create the evidence session")
        session_id = int(row["id"])
        receipt = None
        for text in TURNS:
            if text == "yes":
                backend.configure(ScenarioConfig(book_outcomes=["timeout_booked"]))  # booked, client times out
            reply = agent.handle(session_id, DEMO_USER, text, receipt)
            receipt = reply.receipt_id or receipt
            print(f"> {text}\n" + "\n".join(f"  {m}" for m in reply.messages) + f"\n  [state: {reply.state}]")
        rows = session_events(db, session_id)
        with db.txn() as conn:
            booked = conn.execute(
                "SELECT count(*) AS n FROM mock_bookings WHERE hold_id IS NOT NULL AND status = 'confirmed'"
            ).fetchone()
    finally:
        db.close()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path("artifacts/traces") / f"timeout-after-success-{model.name.replace(':', '-')}-{stamp}.jsonl"
    write_jsonl(out, session_id, rows, note=f"real model {model.name}; book outcome forced to timeout_booked")
    kinds = [r.get("kind") for r in rows if r["type"] == "event"]
    timeouts = [
        r
        for r in rows
        if r.get("kind") == "tool.call"
        and r["payload"].get("tool") == "book"
        and (r["payload"].get("error") or {}).get("type") == "ToolTimeoutError"
    ]
    status_checks = sum(
        1 for r in rows if r.get("kind") == "tool.call" and r["payload"].get("tool") == "get_booking_status"
    )
    print(f"trace: {out} ({len(rows)} rows; {kinds.count('model.call')} model calls)")
    print(
        f"book timed out: {bool(timeouts)}; status checks: {status_checks}; confirmed bookings: "
        f"{booked['n'] if booked else '?'}"
    )
    return 0 if timeouts and status_checks >= 1 and booked and booked["n"] == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
