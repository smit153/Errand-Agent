"""Real-model smoke test (E42, revised for ModelMux; DECISIONS §17).

Runs the three PDF example requests as full conversations through the agent and the configured
ModelMux provider, against seeded mocks, an injected clock and a disposable `errand_smoke`
database. Reports the measured first-pass and after-retry valid-call rates, latency per call and
per flow, the parameters ModelMux ignored, and whether every flow reached its expected end state
without an unapproved side effect. Results are written to artifacts/smoke/. Nothing is claimed
beyond what was measured.

Usage (from the host): scripts/smoke.sh [claude|codex]
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel

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
from errand_agent.workflow.agent import Agent

T0 = datetime(2026, 11, 6, 2, 30, tzinfo=UTC)  # Friday 6 Nov 2026, 08:00 Asia/Kolkata
SMOKE_DB = "errand_smoke"


class Flow(BaseModel):
    name: str
    turns: list[str]
    expected_final_state: str = "completed"


FLOWS = [
    Flow(
        name="plumber",
        turns=["Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.", "Indiranagar", "yes"],
    ),
    Flow(name="ac_reschedule", turns=["Move my AC servicing to Saturday.", "yes"]),
    Flow(
        name="same_electrician",
        turns=["Book the same electrician as last time, but not before 10 a.m.", "Tomorrow, no budget.", "yes"],
    ),
]


def _fresh_database(settings: Settings) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", SMOKE_DB) or settings.db_name == SMOKE_DB:
        raise RuntimeError("refusing to use the smoke database name")
    with psycopg.connect(settings.conninfo(dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(SMOKE_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(SMOKE_DB)))
    info = settings.conninfo(dbname=SMOKE_DB)
    migrate(info)
    return info


def run_flow(db: Database, agent: Agent, flow: Flow, clock: FakeClock) -> dict[str, Any]:
    with db.txn() as conn:
        row = conn.execute(
            "INSERT INTO sessions (user_id, created_at, updated_at) VALUES (%s, %s, %s) RETURNING id",
            (DEMO_USER, clock.now(), clock.now()),
        ).fetchone()
    if row is None:
        raise RuntimeError("could not create a smoke session")
    session_id = int(row["id"])
    receipt = None
    turns: list[dict[str, Any]] = []
    started = time.perf_counter()
    for text in flow.turns:
        t0 = time.perf_counter()
        reply = agent.handle(session_id, DEMO_USER, text, receipt)
        if reply.receipt_id is not None:
            receipt = reply.receipt_id
        turns.append(
            {
                "user": text,
                "state": reply.state,
                "reply": reply.messages,
                "seconds": round(time.perf_counter() - t0, 2),
            }
        )
    final_state = turns[-1]["state"] if turns else "none"
    return {
        "flow": flow.name,
        "session_id": session_id,
        "passed": final_state == flow.expected_final_state,
        "final_state": final_state,
        "seconds": round(time.perf_counter() - started, 2),
        "turns": turns,
    }


def call_metrics(db: Database) -> dict[str, Any]:
    with db.txn() as conn:
        rows = conn.execute("SELECT payload FROM trace_events WHERE kind = 'model.call' ORDER BY id").fetchall()
    calls = [r["payload"] for r in rows]
    logical = [c for c in calls if c["attempt"] == 1]
    first_pass = sum(1 for c in logical if c["accepted"])
    # Each logical call ends accepted at most once; an accepted retry closes its logical call.
    eventually = sum(1 for c in calls if c["accepted"])
    latencies = [float(c["latency_ms"]) for c in calls]
    problems = [c["problem"] for c in calls if c["problem"]]
    ignored = sorted({p for c in calls for p in (c.get("ignored_params") or [])})
    return {
        "logical_calls": len(logical),
        "physical_calls": len(calls),
        "first_pass_valid_rate": round(first_pass / len(logical), 3) if logical else None,
        "after_retries_valid_rate": round(eventually / len(logical), 3) if logical else None,
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "median": round(statistics.median(latencies), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "by_purpose": {
            p: sum(1 for c in logical if c["purpose"] == p) for p in sorted({c["purpose"] for c in logical})
        },
        "problems": problems[:20],
        "ignored_params": ignored,
    }


def invariants(db: Database) -> dict[str, Any]:
    with db.txn() as conn:
        bookings = conn.execute(
            "SELECT count(*) AS n FROM mock_bookings WHERE user_id = %s AND hold_id IS NOT NULL", (DEMO_USER,)
        ).fetchone()
        approved = conn.execute("SELECT count(*) AS n FROM operations WHERE action = 'book'").fetchone()
    if bookings is None or approved is None:
        raise RuntimeError("invariant queries returned no row")
    return {
        "bookings_created": int(bookings["n"]),
        "approved_book_operations": int(approved["n"]),
        "no_booking_without_approval": int(bookings["n"]) <= int(approved["n"]),
    }


def main() -> int:
    settings = load_settings()
    model = build_chat_model(settings)
    db = Database(_fresh_database(settings))
    clock = FakeClock(T0)
    backend = MockBackend(db, clock, seed=settings.mock_seed)
    backend.seed(anchor=T0)
    backend.configure(ScenarioConfig(book_outcomes=["success"] * 3))  # the smoke test measures language, not luck
    memory: MemoryStore | None
    try:
        memory = build_memory(db, clock, settings)
        memory_note = f"on ({memory.embedder.model})"
    except MemoryConfigError as exc:
        memory, memory_note = None, f"off: {exc}"
    agent = Agent(db, clock, ToolClient(backend, db, clock), model, holder="smoke", memory=memory)
    try:
        flows = [run_flow(db, agent, flow, clock) for flow in FLOWS]
        report: dict[str, Any] = {
            "when": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model.name,
            "memory": memory_note,
            "flows": flows,
            "calls": call_metrics(db),
            "invariants": invariants(db),
        }
    finally:
        db.close()
    out_dir = Path("artifacts/smoke")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"smoke-{model.name.replace(':', '-')}-{report['when'].replace(':', '')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    calls: dict[str, Any] = report["calls"]
    print(f"model {model.name}: {sum(f['passed'] for f in flows)}/{len(flows)} flows reached their expected state")
    for f in flows:
        print(f"  {f['flow']:<17} {'PASS' if f['passed'] else 'FAIL'}  final={f['final_state']:<22} {f['seconds']}s")
    print(
        f"  calls: {calls['logical_calls']} logical / {calls['physical_calls']} physical; first-pass valid "
        f"{calls['first_pass_valid_rate']}, after retries {calls['after_retries_valid_rate']}; "
        f"latency median {calls['latency_ms']['median']} ms, max {calls['latency_ms']['max']} ms"
    )
    print(f"  memory: {memory_note}")
    print(f"  ignored by ModelMux: {', '.join(calls['ignored_params']) or 'none reported'}")
    print(f"  invariant no booking without approval: {report['invariants']['no_booking_without_approval']}")
    print(f"  report: {out}")
    return 0 if all(f["passed"] for f in flows) and report["invariants"]["no_booking_without_approval"] else 1


if __name__ == "__main__":
    sys.exit(main())
