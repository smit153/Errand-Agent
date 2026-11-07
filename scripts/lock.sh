#!/usr/bin/env bash
# Regenerate hash-pinned locks (inside the dev container), then verify them against pypi.org on the host:
#   docker compose --profile dev run --rm --no-deps dev scripts/lock.sh
#   python3 -I scripts/verify_lock_hashes.py requirements.lock requirements-dev.lock
set -euo pipefail
common=(--generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28
        --custom-compile-command "scripts/lock.sh" --quiet)
uv pip compile pyproject.toml "${common[@]}" -o requirements.lock
uv pip compile pyproject.toml --group dev "${common[@]}" -o requirements-dev.lock
