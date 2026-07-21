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

# auth-data/ is fully gitignored -- a fresh clone has no such directory
# at all. If Docker (running as root) is the first thing to ever touch
# this path (i.e. docker-compose/terraform ran before
# extract-opencode-key.sh ever did), it creates the WHOLE chain as
# root while bind-mounting, not just the phantom auth.json leaf --
# confirmed live: clearing the leaf directory alone still left the
# parent unwritable by a normal user, so the subsequent extraction
# write failed too. Reclaim ownership of the parent if needed, before
# touching the leaf.
if [ -d "${AUTH_DATA_DIR}" ] && [ ! -w "${AUTH_DATA_DIR}" ]; then
  echo "ensure-auth-data.sh: ${AUTH_DATA_DIR} isn't writable by you --" >&2
  echo "  same root-ownership cause as the phantom-directory case (Docker" >&2
  echo "  created the whole path while bind-mounting). Reclaiming ownership" >&2
  echo "  with sudo (will prompt for your password)..." >&2
  sudo chown "$(id -un):$(id -gn)" "${AUTH_DATA_DIR}"
fi

if [ -d "${AUTH_FILE}" ]; then
  echo "ensure-auth-data.sh: ${AUTH_FILE} is a directory -- this is Docker's" >&2
  echo "  phantom-mount bug (a bind-mount source that didn't exist yet), not" >&2
  echo "  real content. Removing it via rmdir (refuses if non-empty)." >&2

  # Capture stderr into a variable rather than a temp file -- avoids a
  # cleanup/collision concern for a script that might run concurrently.
  # Two distinct failure modes here, and they need different responses:
  #   - "Permission denied": the Docker daemon runs as root and created
  #     this directory (and often its parent auth-data/ too) as root --
  #     hit live on Cyberdyne. A non-root user genuinely cannot rmdir
  #     it without sudo, no matter how empty it is. Retry with sudo.
  #   - Anything else (e.g. "Directory not empty"): rmdir refusing here
  #     means this ISN'T actually the phantom-mount case -- there's
  #     real content Terraform/Compose never should have touched. Do
  #     NOT force-delete; surface it and let the user look themselves.
  if ! rmdir_err="$(rmdir "${AUTH_FILE}" 2>&1)"; then
    if echo "${rmdir_err}" | grep -qi "permission denied"; then
      echo "ensure-auth-data.sh: permission denied -- this directory was created" >&2
      echo "  by the Docker daemon (runs as root), so removing it needs root too," >&2
      echo "  regardless of how empty it is. Retrying with sudo (will prompt for" >&2
      echo "  your password)..." >&2
      if ! sudo_err="$(sudo rmdir "${AUTH_FILE}" 2>&1)"; then
        echo "ensure-auth-data.sh: rmdir still failed even with sudo:" >&2
        echo "  ${sudo_err}" >&2
        echo "  If that says 'not empty', this wasn't actually the phantom-mount" >&2
        echo "  case -- there may be real content here. Not deleting anything" >&2
        echo "  automatically. Inspect it yourself: sudo ls -la ${AUTH_FILE}" >&2
        exit 1
      fi
    else
      echo "ensure-auth-data.sh: rmdir failed for a reason other than permissions:" >&2
      echo "  ${rmdir_err}" >&2
      echo "  This means it probably ISN'T the phantom-mount case -- there may be" >&2
      echo "  real content here. Not deleting anything automatically. Inspect it" >&2
      echo "  yourself: ls -la ${AUTH_FILE}" >&2
      exit 1
    fi
  fi
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
