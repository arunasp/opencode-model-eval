#!/bin/bash
# tf-extract-auth-keys.sh -- wraps extract-opencode-key.sh for use as a
# Terraform `data "external"` program.
#
# CRITICAL CONTRACT, not a style choice: whatever this script prints to
# stdout becomes data.external.<name>.result, and EVERY result value is
# stored in terraform.tfstate in plaintext (confirmed against
# hashicorp/external's own docs: "All output values are stored in the
# Terraform state file"). This script must NEVER print real key
# material to stdout -- only a confirmation that extraction succeeded
# and which provider keys were included. The actual secret write
# happens entirely inside extract-opencode-key.sh, writing directly to
# auth-data/auth.json on disk; this wrapper never reads that file's
# contents back into a variable, only checks its exit code.
#
# External data source protocol (hashicorp/external provider):
#   - stdin: JSON object (the `query` block), all values are strings
#   - stdout on success: JSON object, all values strings, exit 0
#   - stderr + exit nonzero on failure (stdout must NOT be used for
#     error text -- it would break the caller's JSON parsing)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly EXTRACT_SCRIPT="${SCRIPT_DIR}/extract-opencode-key.sh"

# Read the full query object from stdin, extract the "keys" field
# (comma-separated provider names, e.g. "opencode,deepseek,zhipu").
query_json="$(cat)"
keys_csv="$(echo "${query_json}" | jq -r '.keys // empty')"

if [ -z "${keys_csv}" ]; then
  echo "tf-extract-auth-keys.sh: query.keys was empty or missing -- expected a comma-separated provider list" >&2
  exit 1
fi

if [ ! -x "${EXTRACT_SCRIPT}" ]; then
  echo "tf-extract-auth-keys.sh: ${EXTRACT_SCRIPT} not found or not executable" >&2
  exit 1
fi

# Split on commas into positional args for extract-opencode-key.sh.
IFS=',' read -r -a keys_array <<< "${keys_csv}"

if ! "${EXTRACT_SCRIPT}" "${keys_array[@]}" >/tmp/tf-extract-auth-keys.stdout.log 2>/tmp/tf-extract-auth-keys.stderr.log; then
  echo "tf-extract-auth-keys.sh: extract-opencode-key.sh failed for keys [${keys_csv}]. Its stderr:" >&2
  cat /tmp/tf-extract-auth-keys.stderr.log >&2
  exit 1
fi

# Confirmation only -- deliberately NOT the file's contents, NOT any
# key value. keys_csv is provider NAMES ("opencode", "deepseek"), not
# secrets, safe to echo back and safe to land in state.
jq -n --arg keys "${keys_csv}" '{"status": "ok", "keys_extracted": $keys}'
