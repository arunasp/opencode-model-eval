#!/bin/bash
# Extracts provider entries from your real opencode auth.json into a
# separate, minimal file under auth-data/ — narrower than mounting your
# whole ~/.local/share/opencode directory.
#
# Runs entirely on the host, outside Docker Compose and Terraform, on
# purpose — the key's actual value never gets read into Terraform state
# or a Compose env file.
#
# Usage:
#   bash scripts/extract-opencode-key.sh                     # lists available keys
#   bash scripts/extract-opencode-key.sh --all                # writes ALL keys verbatim
#   bash scripts/extract-opencode-key.sh <key>                # writes auth-data/auth.json with ONLY that key
#   bash scripts/extract-opencode-key.sh <key1> <key2> ...     # writes auth-data/auth.json with exactly those keys
#
# --all exists because model selection is no longer a fixed matrix (see
# terraform/main.tf's docker_container.discover comment) -- which
# provider a given run actually needs is resolved live via `opencode
# models --verbose` at eval time, not known ahead of the auth-scoping
# step. Scoping to "every key you have configured" is still narrower
# than the alternative of mounting the real, unscoped auth.json
# directly.
set -euo pipefail

readonly SOURCE="${OPENCODE_AUTH_SOURCE:-$HOME/.local/share/opencode/auth.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly DEST_DIR="${SCRIPT_DIR}/../auth-data"
readonly DEST="${DEST_DIR}/auth.json"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required for this script (not found on PATH)." >&2
  exit 1
fi

if [ ! -f "${SOURCE}" ]; then
  echo "No auth.json found at ${SOURCE} — nothing to extract." >&2
  echo "Set OPENCODE_AUTH_SOURCE if it lives somewhere else on this host." >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  echo "Available provider keys in ${SOURCE}:"
  jq -r 'keys[]' "${SOURCE}"
  echo ""
  echo "Re-run with one of these, or --all: $0 <provider-key-name>"
  exit 0
fi

mkdir -p "${DEST_DIR}"

if [ "$1" = "--all" ]; then
  cp "${SOURCE}" "${DEST}"
  chmod 600 "${DEST}"
  key_count="$(jq 'keys | length' "${DEST}")"
  echo "Wrote a scoped auth.json (all ${key_count} configured provider key(s)) to: ${DEST}"
  echo "Docker Compose and the Terraform config both point at this path already."
  exit 0
fi

# Fail loudly (non-zero exit, nothing written) if ANY requested key is
# missing, rather than silently writing a partial file — a partially
# scoped auth.json that's missing one provider fails at container
# runtime in a much less obvious way than failing here, up front.
missing_keys=()
for key in "$@"; do
  if ! jq -e --arg k "${key}" 'has($k)' "${SOURCE}" >/dev/null; then
    missing_keys+=("${key}")
  fi
done

if [ "${#missing_keys[@]}" -gt 0 ]; then
  echo "Key(s) not found in ${SOURCE}: ${missing_keys[*]}" >&2
  echo "Run this script with no arguments to list what's actually there." >&2
  exit 1
fi

jq_filter_args=()
jq_filter_parts=()
i=0
for key in "$@"; do
  i=$((i + 1))
  jq_filter_args+=(--arg "k${i}" "${key}")
  jq_filter_parts+=("(\$k${i}): .[\$k${i}]")
done
jq_filter="{$(IFS=,; echo "${jq_filter_parts[*]}")}"

jq "${jq_filter_args[@]}" "${jq_filter}" "${SOURCE}" > "${DEST}"
chmod 600 "${DEST}"
echo "Wrote a scoped auth.json (key(s): $*) to: ${DEST}"
echo "Docker Compose and the Terraform config both point at this path already."

