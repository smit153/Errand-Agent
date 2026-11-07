#!/usr/bin/env bash
# Real-model smoke test (E42, revised): the three PDF examples through ModelMux. Report: artifacts/smoke/.
set -euo pipefail
exec "$(dirname "$0")/with_model.sh" "${1:-claude}" errand_agent.smoke
