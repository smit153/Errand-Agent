#!/usr/bin/env bash
# Run a command in the dev container, e.g. scripts/dev.sh scripts/check.sh -x
set -euo pipefail
cd "$(dirname "$0")/.."
# The commit under test is passed in for generated reports (the container has no git).
commit="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)$(git diff --quiet HEAD 2>/dev/null || echo +dirty)"
exec docker compose --profile dev run --rm -T -e ERRAND_GIT_COMMIT="$commit" dev "$@"
