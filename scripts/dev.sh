#!/usr/bin/env bash
# Run a command in the dev container, e.g. scripts/dev.sh scripts/check.sh -x
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose --profile dev run --rm -T dev "$@"
