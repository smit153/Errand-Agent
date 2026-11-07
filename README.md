# Errand Agent

A local, chat-based agent that arranges home-service errands: it clarifies missing details,
compares providers, asks for approval before consequential actions, and recovers safely from tool
failures. Everything runs locally with mock providers; there are no real providers, payments or
external services. The brief is `Orange_Engineering_Assignment.pdf`; the design is
[docs/DESIGN.md](docs/DESIGN.md), with decisions and the build plan in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Status

Stages 1–3 of the 8-stage build plan are done (see [PROGRESS.md](PROGRESS.md)):

1. PostgreSQL 16 + pgvector schema, migrations, test harness, durable claims with generation fencing.
2. All eight fixed mock tools with seeded, configurable failure behaviour.
3. The code-enforced approval gate, the operation executor (exact-snapshot dispatch, forced
   source-of-truth verification, same-key retries, limits) and crash recovery.

The language model, memory/context, background workers, trace export/eval table and the CLI are
not built yet. Real bugs found so far are in [docs/BUGS.md](docs/BUGS.md).

## Layout

| Path | Contents |
| --- | --- |
| `errand_agent/db/` | Connection pool, per-session advisory-lock transactions, migrations |
| `errand_agent/mocks/` | Seeded provider catalog and the Postgres-backed mock backend |
| `errand_agent/tools/` | The eight tool contracts (Pydantic models) and `ToolClient` |
| `errand_agent/workflow/` | State machine, approval gate, executor, budgets, recovery, templates |
| `tests/` | Invariant and contract tests against real Postgres |

## Setup

Requirements: Docker with Compose. The Python 3.12 toolchain runs in a container, so no local
Python install is needed.

```bash
cp .env.example .env            # then set ERRAND_DB_PASSWORD to a random value
docker compose up -d --wait postgres
docker compose --profile dev build dev
scripts/dev.sh scripts/check.sh # hash-verified install, ruff, mypy --strict, pytest
```

Postgres listens on `127.0.0.1:5433` only. Tests create and drop their own `errand_test` database
on that server. Pass pytest arguments through, e.g. `scripts/dev.sh scripts/check.sh -k approval`.

## Dependencies, the package mirror and hash verification

Images and packages are pinned:

- **Images** are pinned by digest: `pgvector/pgvector:0.8.1-pg16` (PostgreSQL 16.12) and
  `python:3.12-slim-bookworm` (Python 3.12.16).
- **Python packages** are pinned in `requirements.lock` (runtime) and `requirements-dev.lock`
  (runtime + dev). Every entry has sha256 hashes, and every install uses `--require-hashes`. The
  uv bootstrap in the dev image is hash-pinned too (`docker/uv-requirements.txt`).

On the machine this was built on, downloads from PyPI's CDN (`files.pythonhosted.org`) ran at about
9 KB/s. So, with the developer's approval, packages were downloaded from the **Tencent PyPI mirror**
(`https://mirrors.cloud.tencent.com/pypi/simple/`). A mirror is safe here only because of the
hashes:

- `scripts/verify_lock_hashes.py` checks every hash in both lock files against PyPI's own JSON
  metadata on `pypi.org`. On 2026-11-07 it checked all 31 pinned packages with 0 failures. A
  mirror cannot substitute a file, because an altered file fails `--require-hashes`.
- The mirror URL appears only in the local, git-ignored `.env` (`PIP_INDEX_URL`,
  `UV_DEFAULT_INDEX`). The committed config defaults to `https://pypi.org/simple`, so on a normal
  network nothing needs changing.

To change dependencies, edit `pyproject.toml`, then re-lock and verify before installing:

```bash
scripts/dev.sh scripts/lock.sh
python3 -I scripts/verify_lock_hashes.py requirements.lock requirements-dev.lock   # must report 0 failures
```

## Configuration

All configuration comes from environment variables (`ERRAND_*`, see `.env.example`). No secret
has a default in code.
