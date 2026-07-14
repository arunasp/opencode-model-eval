#!/bin/bash
# entrypoint.sh — runs the fixed task suite against one configured model
# inside the harness image, one prompt file per task, and writes
# artifact-backed results (raw output + SHA-256 + timestamp) per task.
set -euo pipefail

readonly PROMPTS_DIR="/task-suite/prompts"
readonly RESULTS_ROOT="/results"
readonly AUTH_PATH="${HOME}/.local/share/opencode/auth.json"

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

export OPENCODE_MODEL="${OPENCODE_MODEL_PROVIDER}/${OPENCODE_MODEL_ID}"
readonly MODEL_SLUG="${OPENCODE_MODEL_PROVIDER}_${OPENCODE_MODEL_ID//[:\/]/-}"
readonly RESULTS_DIR="${RESULTS_ROOT}/${MODEL_SLUG}"

log "model under test: ${OPENCODE_MODEL}"
log "HOME resolved to: ${HOME}"

if [ ! -f "${AUTH_PATH}" ]; then
  fail "credentials not found at ${AUTH_PATH} — mount your host auth.json read-only to this path (see README)"
fi

if ! command -v opencode >/dev/null 2>&1; then
  fail "opencode binary not found on PATH — base image contract has changed, re-verify against opencode.ai/docs"
fi

mkdir -p "${RESULTS_DIR}"

# Provenance: record exactly what ran this batch, once per container run.
{
  printf 'model: %s\n' "${OPENCODE_MODEL}"
  printf 'opencode_version: %s\n' "$(opencode --version 2>&1)"
  printf 'run_started_utc: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
} > "${RESULTS_DIR}/run-manifest.txt"

if [ ! -d "${PROMPTS_DIR}" ] || [ -z "$(find "${PROMPTS_DIR}" -maxdepth 1 -name '*.txt' -print -quit)" ]; then
  fail "no task prompts found under ${PROMPTS_DIR} — the fixed task suite has not been populated yet; this harness runs it, it does not author it"
fi

shopt -s nullglob
task_count=0
for prompt_file in "${PROMPTS_DIR}"/*.txt; do
  task_count=$((task_count + 1))
  task_name="$(basename "${prompt_file}" .txt)"
  output_file="${RESULTS_DIR}/${task_name}.json"
  raw_file="${RESULTS_DIR}/${task_name}.raw.txt"
  timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

  log "running task ${task_count}: ${task_name}"

  prompt_text="$(cat "${prompt_file}")"

  if opencode run "${prompt_text}" --model "${OPENCODE_MODEL}" --format json > "${raw_file}" 2>"${RESULTS_DIR}/${task_name}.stderr.log"; then
    task_status="ok"
  else
    task_status="failed"
    log "task ${task_name} exited non-zero — see ${task_name}.stderr.log"
  fi

  content_hash="$(sha256sum "${raw_file}" | awk '{print $1}')"

  jq -n \
    --arg task "${task_name}" \
    --arg model "${OPENCODE_MODEL}" \
    --arg status "${task_status}" \
    --arg captured_utc "${timestamp}" \
    --arg sha256 "${content_hash}" \
    --arg raw_path "${raw_file}" \
    '{task: $task, model: $model, status: $status, captured_utc: $captured_utc, sha256: $sha256, raw_path: $raw_path}' \
    > "${output_file}"
done

if [ "${task_count}" -eq 0 ]; then
  fail "prompts directory existed but contained no .txt files"
fi

log "completed ${task_count} task(s) for ${OPENCODE_MODEL} — results in ${RESULTS_DIR}"
