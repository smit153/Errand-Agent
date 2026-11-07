#!/usr/bin/env bash
# Real-model evidence run: one booking with a timeout after success; raw trace to artifacts/traces/.
set -euo pipefail
exec "$(dirname "$0")/with_model.sh" "${1:-codex}" errand_agent.evidence
