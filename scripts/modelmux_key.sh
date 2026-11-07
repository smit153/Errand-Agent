#!/usr/bin/env bash
# Print the ModelMux gateway key from its secrets file, for use only in a command substitution.
# ModelMux writes exactly one line, MODELMUX_API_KEYS=<base64url, >= 32 chars> (modelmux-cli
# secretsStore.js). Anything else is rejected without echoing the file's contents.
set -euo pipefail
file="${1:?usage: modelmux_key.sh <secrets.env>}"
[ -r "$file" ] || { echo "cannot read $file: run 'modelmux up' first" >&2; exit 2; }
key="$(sed -n 's/^MODELMUX_API_KEYS=\([A-Za-z0-9_-]\{32,\}\)$/\1/p' "$file")"
lines="$(grep -cv '^[[:space:]]*$' "$file" || true)"
if [ -z "$key" ] || [ "$lines" != 1 ]; then
  echo "no valid MODELMUX_API_KEYS line in $file" >&2
  exit 2
fi
printf '%s' "$key"
