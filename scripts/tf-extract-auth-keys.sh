#!/bin/bash
# tf-extract-auth-keys.sh -- wraps extract-opencode-key.sh for use as a
# Terraform `data "external"` program.
#
# CRITICAL CONTRACT, not a style choice: whatever this script prints to
# stdout becomes data.external.<name>.result, and EVERY result value is
# stored in terraform.tfstate in plaintext (confirmed against
# hashicorp/external's own docs: "All output values are stored in the
# Terraform state file"). This script must NEVER print real key
# material to stdout -- only a confirmation that extraction succeeded.
# The actual secret write happens entirely inside extract-opencode-key.sh,
# writing directly to auth-data/auth.json on disk; this wrapper never
# reads that file's contents back into a variable, only checks its
# exit code.
#
# Always extracts --all now, not a provider list derived from a static
# model matrix -- there is no such matrix anymore (see
# terraform/main.tf's docker_container.discover comment). Which
# provider a given eval run actually needs is resolved live via
# `opencode models --verbose` at run time, so the auth-scoping step
# can't know that set in advance; scoping to "every key you currently
# have configured" is the closest equivalent that still stays narrower
# than mounting the real, unscoped auth.json.
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

# Read (and discard) the query object -- data "external" requires
# stdin be consumed even though this wrapper no longer needs any of
# its fields.
cat >/dev/null

if [ ! -x "${EXTRACT_SCRIPT}" ]; then
  echo "tf-extract-auth-keys.sh: ${EXTRACT_SCRIPT} not found or not executable" >&2
  exit 1
fi

if ! "${EXTRACT_SCRIPT}" --all >/tmp/tf-extract-auth-keys.stdout.log 2>/tmp/tf-extract-auth-keys.stderr.log; then
  echo "tf-extract-auth-keys.sh: extract-opencode-key.sh --all failed. Its stderr:" >&2
  cat /tmp/tf-extract-auth-keys.stderr.log >&2
  exit 1
fi

# Confirmation only -- deliberately NOT the file's contents, NOT any
# key value.
jq -n '{"status": "ok", "mode": "all"}'
