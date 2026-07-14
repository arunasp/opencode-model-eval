#!/bin/bash
# Extracts ONE provider's entry from your real opencode auth.json into a
# separate, minimal file under auth-data/ — narrower than mounting your
# whole ~/.local/share/opencode directory, and narrower than pointing at
# the whole real auth.json (which may hold every provider you've ever
# connected, per your actual file: xai, groq, opencode, google,
# opencode-go, openrouter, huggingface, nvidia, poolside).
#
# Runs entirely on the host, outside Docker Compose and Terraform, on
# purpose — the key's actual value never gets read into Terraform state
# or a Compose env file.
#
# Usage:
#   bash scripts/extract-opencode-key.sh                     # lists available keys
#   bash scripts/extract-opencode-key.sh <key>                # writes auth-data/auth.json with ONLY that key
#   bash scripts/extract-opencode-key.sh <key1> <key2> ...     # writes auth-data/auth.json with exactly those keys
#
# This project's model matrix spans several providers at once
# (opencode-zen, deepseek, zhipu — see terraform/variables.tf's
# `models` map), and every container mounts the same auth-data/auth.json.
# A single-key file would only authorize one of them; pass every
# provider key your current matrix actually needs.
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
  echo "Re-run with one of these: $0 <provider-key-name>"
  exit 0
fi

mkdir -p "${DEST_DIR}"

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
