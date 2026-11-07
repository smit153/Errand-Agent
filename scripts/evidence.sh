#!/usr/bin/env bash
# Real-model evidence run: one booking with a timeout after success; exports the raw trace.
set -euo pipefail
cd "$(dirname "$0")/.."
ERRAND_REAL_MODEL_MODULE=errand_agent.evidence exec scripts/smoke.sh "${1:-codex}"
