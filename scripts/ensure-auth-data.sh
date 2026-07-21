#!/bin/bash
# ensure-auth-data.sh -- compose-path equivalent of what Terraform's
# data.external.auth_keys + terraform_data.auth_file_check already do
# automatically. docker-compose has no precondition/pre-flight hook
# mechanism (confirmed: v1.29.2 here is the legacy CLI, no such
# feature), so this has to be a script you run yourself before
# `docker-compose up`/`run`, rather than something compose triggers on
# its own. Root-cause fix, not another manual rmdir: run this once
# before compose commands and the phantom-directory bug can't recur
# for this path either.
#
# What it does, in order:
#   1. If auth-data/auth.json is a directory (the phantom-mount bug --
#      Docker silently creates an empty dir at a bind-mount source path
#      that doesn't exist yet, rather than erroring): remove it, but
#      only via rmdir, which refuses on anything non-empty rather than
#      silently deleting real content.
#   2. If auth-data/auth.json doesn't exist as a file after that: run
#      extract-opencode-key.sh with whatever provider keys you passed.
#   3. If it's already a real file: no-op, confirm and exit -- doesn't
#      re-extract/overwrite an existing valid file just because you
#      ran this again.
#
# Usage:
#   bash scripts/ensure-auth-data.sh                     # lists available keys, does nothing else
#   bash scripts/ensure-auth-data.sh opencode deepseek zhipu   # fixes phantom dir if present, extracts these keys
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly EXTRACT_SCRIPT="${SCRIPT_DIR}/extract-opencode-key.sh"
readonly AUTH_DATA_DIR="${SCRIPT_DIR}/../auth-data"
readonly AUTH_FILE="${AUTH_DATA_DIR}/auth.json"

if [ -d "${AUTH_FILE}" ]; then
  echo "ensure-auth-data.sh: ${AUTH_FILE} is a directory -- this is Docker's" >&2
  echo "  phantom-mount bug (a bind-mount source that didn't exist yet), not" >&2
  echo "  real content. Removing it via rmdir (refuses if non-empty)." >&2
  rmdir "${AUTH_FILE}"
  echo "ensure-auth-data.sh: phantom directory removed." >&2
fi

if [ -f "${AUTH_FILE}" ]; then
  echo "ensure-auth-data.sh: ${AUTH_FILE} already exists as a real file -- leaving it as-is." >&2
  echo "  (delete it yourself first if you want to re-extract with different keys.)" >&2
  exit 0
fi

if [ "$#" -eq 0 ]; then
  echo "ensure-auth-data.sh: no provider keys given -- listing what's available" >&2
  echo "  without writing anything. Re-run with keys to actually extract:" >&2
  echo "    bash $0 opencode deepseek zhipu" >&2
  echo >&2
  "${EXTRACT_SCRIPT}"
  exit 0
fi

echo "ensure-auth-data.sh: extracting keys [$*] into ${AUTH_FILE}..." >&2
"${EXTRACT_SCRIPT}" "$@"
