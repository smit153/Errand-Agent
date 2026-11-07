#!/usr/bin/env bash
# Run an app module with the real model through ModelMux:
#   scripts/with_model.sh <claude|codex> <errand_agent.smoke|errand_agent.evidence|errand_agent.serve> [args...]
# Prerequisites: `docker compose up -d --wait postgres`, `modelmux up` + `modelmux login <provider>`;
# optionally `docker compose --profile embed up -d --wait ollama` for memory.
# The gateway key is read from ModelMux's own secrets file into the environment, never an argument.
set -euo pipefail
cd "$(dirname "$0")/.."
provider="${1:-}"; module="${2:-}"
case "$provider" in claude|codex) ;; *) echo "usage: scripts/with_model.sh <claude|codex> <module> [args...]" >&2; exit 2 ;; esac
case "$module" in errand_agent.smoke|errand_agent.evidence|errand_agent.serve) ;; *) echo "unknown module '$module'" >&2; exit 2 ;; esac
shift 2
ERRAND_MODELMUX_API_KEY="$(scripts/modelmux_key.sh "$HOME/.config/modelmux/secrets.env")"
export ERRAND_MODELMUX_API_KEY ERRAND_MODELMUX_PROVIDER="$provider"
docker compose --profile dev build -q dev
tty=(); [ -t 0 ] && tty=(-t)
# Host networking so the container reaches ModelMux, Postgres and Ollama on this machine's loopback.
exec docker run --rm --init "${tty[@]}" --network host --user "$(id -u):$(id -g)" \
  --env-file .env -e ERRAND_MODELMUX_API_KEY -e ERRAND_MODELMUX_PROVIDER -e ERRAND_DB_HOST=127.0.0.1 \
  -e ERRAND_EMBED_URL=http://127.0.0.1:11434 \
  -v "$PWD":/app -v errand-agent_errand-venv:/opt/venv -v errand-agent_errand-uv-cache:/opt/uv-cache \
  errand-agent-dev bash -c 'uv pip sync --require-hashes --quiet requirements-dev.lock && exec python -m "$0" "$@"' \
  "$module" "$@"
