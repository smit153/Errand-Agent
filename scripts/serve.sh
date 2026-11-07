#!/usr/bin/env bash
# Run the app (HTTP API + agent + background worker) on http://127.0.0.1:8765 with the real model:
#   scripts/serve.sh codex --db errand_demo --fresh --demo-controls [--seed-memories]
# Then, in two other terminals on this machine: python3 -m errand_agent.cli chat / trace --follow
set -euo pipefail
provider="${1:-codex}"; shift || true
exec "$(dirname "$0")/with_model.sh" "$provider" errand_agent.serve "$@"
