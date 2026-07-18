#!/bin/bash
# entrypoint.sh — validates environment/credentials, then delegates to
# run_test_ladder.py for the actual structured, escalating-difficulty
# test execution (task-suite/test_ladder.json). Kept thin deliberately:
# bash is the wrong tool for JSON-event parsing, session threading, and
# escalation logic -- that all lives in Python now, where cvv_scan.py
# already does the same class of work.
set -euo pipefail

readonly RESULTS_ROOT="/results"
readonly AUTH_PATH="${HOME}/.local/share/opencode/auth.json"
readonly TASK_LADDER="/task-suite/test_ladder.json"

log() {
  printf '[entrypoint] %s\n' "$1" >&2
}

fail() {
  log "FATAL: $1"
  exit 1
}

require_env() {
  local var_name="$1"
  if [ -z "${!var_name:-}" ]; then
    fail "required environment variable ${var_name} is not set"
  fi
}

require_env OPENCODE_MODEL_PROVIDER
require_env OPENCODE_MODEL_ID

readonly OPENCODE_MODEL="${OPENCODE_MODEL_PROVIDER}/${OPENCODE_MODEL_ID}"
readonly MODEL_SLUG="${OPENCODE_MODEL_PROVIDER}_${OPENCODE_MODEL_ID//[:\/]/-}"
readonly RESULTS_DIR="${RESULTS_ROOT}"

log "model under test: ${OPENCODE_MODEL}"
log "HOME resolved to: ${HOME}"

if [ ! -f "${AUTH_PATH}" ]; then
  fail "credentials not found at ${AUTH_PATH} — mount your host auth.json read-only to this path (see README)"
fi

if ! command -v opencode >/dev/null 2>&1; then
  fail "opencode binary not found on PATH — base image contract has changed, re-verify against opencode.ai/docs"
fi

if [ ! -f "${TASK_LADDER}" ]; then
  fail "no test ladder found at ${TASK_LADDER} — mount task-suite/ (containing test_ladder.json) to /task-suite"
fi

mkdir -p "${RESULTS_DIR}"

# Provenance: record exactly what ran this batch, once per container run.
{
  printf 'model: %s\n' "${OPENCODE_MODEL}"
  printf 'opencode_version: %s\n' "$(opencode --version 2>&1)"
  printf 'run_started_utc: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf 'model_slug: %s\n' "${MODEL_SLUG}"
} > "${RESULTS_DIR}/run-manifest.txt"

log "delegating to run_test_ladder.py for structured execution"
python3 /usr/local/bin/run_test_ladder.py
