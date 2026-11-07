"""Background worker process: `python -m errand_agent.workers [--once] [--interval SECONDS]`."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time

from errand_agent.config import load_settings
from errand_agent.runtime import SchemaNotCurrentError, build_runtime
from errand_agent.workers.runner import Runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m errand_agent.workers", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between ticks (default 1)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("errand_agent.workers")
    try:
        runtime = build_runtime(load_settings())
    except SchemaNotCurrentError as exc:
        log.error("%s", exc)  # noqa: TRY400 - an expected setup condition, not a crash: no traceback
        return 2
    holder = f"worker-{socket.gethostname()}-{os.getpid()}"
    runner = Runner(runtime.watcher, runtime.worker(holder))
    log.info("worker %s started; memory %s", holder, runtime.memory_note)
    try:
        while True:
            tick = runner.run_once()
            for job in tick.jobs:
                log.info("job %s %s: %s (%s)", job.kind, job.job_id, job.status, job.detail)
            if tick.ingested:
                log.info("ingested %d provider event(s)", tick.ingested)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("worker %s stopping", holder)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.exit(main())
