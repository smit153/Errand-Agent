"""Run the local app: HTTP API + agent + a background worker thread (stage 8).

    python -m errand_agent.serve [--db errand_demo --fresh] [--seed-memories] [--demo-controls]

Binds loopback only. Applies pending migrations, seeds the mock providers (idempotent), and
starts one worker thread (more workers can run as `python -m errand_agent.workers`). `--fresh`
recreates a demo database whose name starts with `errand_demo`; it never touches another one.
The real model comes from ModelMux (see scripts/serve.sh, which passes the gateway key).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import threading

import psycopg
import uvicorn
from psycopg import sql

from errand_agent.api.app import create_app
from errand_agent.config import Settings, load_settings
from errand_agent.db.migrate import migrate
from errand_agent.llm.factory import build_chat_model
from errand_agent.localnet import check_local
from errand_agent.memory.seed import seed_memories, seed_prior_conversation
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.runtime import Runtime, build_runtime
from errand_agent.workers.runner import Runner
from errand_agent.workflow.agent import Agent

log = logging.getLogger("errand_agent.serve")


def _fresh(settings: Settings, name: str) -> None:
    if not re.fullmatch(r"errand_demo[a-z0-9_]{0,40}", name) or name == settings.db_name:
        raise SystemExit(f"--fresh only recreates databases named errand_demo*, not {name!r}")
    with psycopg.connect(settings.conninfo(dbname="postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _worker_loop(runtime: Runtime, stop: threading.Event) -> None:
    runner = Runner(runtime.watcher, runtime.worker(f"server-{socket.gethostname()}-{os.getpid()}"))
    while not stop.is_set():
        try:
            tick = runner.run_once()
            for job in tick.jobs:
                log.info("job %s: %s (%s)", job.kind, job.status, job.detail)
        except Exception:  # keep the loop alive; the failure is logged with its traceback
            log.exception("background worker tick failed")
        stop.wait(1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m errand_agent.serve", description=__doc__)
    parser.add_argument("--db", help="database name (default from settings)")
    parser.add_argument("--fresh", action="store_true", help="recreate an errand_demo* database first")
    parser.add_argument("--seed-memories", action="store_true", help="seed 50+ memories and a prior conversation")
    parser.add_argument("--demo-controls", action="store_true", help="enable /demo endpoints that steer the mocks")
    parser.add_argument("--no-worker", action="store_true", help="don't run the background worker thread")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    check_local(f"http://{settings.api_host}:{settings.api_port}", what="API")
    dbname = args.db or settings.db_name
    if args.fresh:
        _fresh(settings, dbname)
    applied = migrate(settings.conninfo(dbname=dbname))
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    runtime = build_runtime(settings, dbname=dbname)
    runtime.backend.seed(anchor=runtime.clock.now())
    if args.seed_memories:
        if runtime.memory is None:
            raise SystemExit(f"--seed-memories needs the embedder; memory is {runtime.memory_note}")
        seed_memories(runtime.db, runtime.clock, runtime.memory, DEMO_USER)
        seed_prior_conversation(runtime.db, runtime.clock, DEMO_USER)
    model = build_chat_model(settings)
    agent = Agent(runtime.db, runtime.clock, runtime.tools, model, memory=runtime.memory)
    app = create_app(
        db=runtime.db,
        clock=runtime.clock,
        agent=agent,
        outbox=runtime.outbox,
        backend=runtime.backend,
        user_id=settings.local_user,
        demo_controls=settings.demo_controls or args.demo_controls,
        info={"model": model.name, "memory": runtime.memory_note, "database": dbname},
    )
    stop = threading.Event()
    worker = None if args.no_worker else threading.Thread(target=_worker_loop, args=(runtime, stop), daemon=True)
    if worker:
        worker.start()
    log.info(
        "serving on http://%s:%d (model %s; memory %s)",
        settings.api_host,
        settings.api_port,
        model.name,
        runtime.memory_note,
    )
    try:
        uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="warning")
    finally:
        stop.set()
        if worker:
            worker.join(timeout=5)
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
