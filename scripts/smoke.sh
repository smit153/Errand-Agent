#!/usr/bin/env bash
# Real-model smoke test (E42, revised): the three PDF examples through ModelMux.
# Prerequisites: `docker compose up -d --wait postgres`, and `modelmux login <provider>` + `modelmux up`.
# The gateway key is read from ModelMux's own secrets file and passed by environment, never as an argument.
set -euo pipefail
cd "$(dirname "$0")/.."
provider="${1:-claude}"
case "$provider" in claude|codex) ;; *) echo "usage: scripts/smoke.sh [claude|codex]" >&2; exit 2 ;; esac
ERRAND_MODELMUX_API_KEY="$(scripts/modelmux_key.sh "$HOME/.config/modelmux/secrets.env")"
export ERRAND_MODELMUX_API_KEY ERRAND_MODELMUX_PROVIDER="$provider"
docker compose --profile dev build -q dev
# Host networking so the container reaches ModelMux and Postgres on this machine's loopback.
exec docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file .env -e ERRAND_MODELMUX_API_KEY -e ERRAND_MODELMUX_PROVIDER -e ERRAND_DB_HOST=127.0.0.1 \
  -v "$PWD":/app -v errand-agent_errand-venv:/opt/venv -v errand-agent_errand-uv-cache:/opt/uv-cache \
  errand-agent-dev bash -c 'uv pip sync --require-hashes --quiet requirements-dev.lock && python -m errand_agent.smoke'
