#!/usr/bin/env bash
# Lint, type-check and test inside the dev container:
#   docker compose --profile dev run --rm dev scripts/check.sh [pytest args]
set -euo pipefail
uv pip sync --require-hashes --quiet requirements-dev.lock
ruff format --check errand_agent tests scripts
ruff check errand_agent tests scripts
mypy
pytest "$@"
