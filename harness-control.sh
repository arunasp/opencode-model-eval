#!/bin/bash
# harness-control.sh -- top-level control UI for opencode-model-eval.
# Run from the repo root: bash harness-control.sh
#
# Three real actions, each asks which backend first (both Terraform
# and Docker Compose are fully supported paths in this repo, kept in
# parallel on purpose -- see README's "Two deployment paths" section):
#   - Deploy harness  -> `make tf-apply` or `docker-compose up -d server`
#   - Remove harness  -> `make tf-destroy` or `docker-compose down`
#   - Run an eval     -> delegates to scripts/tf-select-and-run-eval.sh
#                        or scripts/select-and-run-eval.sh, which
#                        already handle provider/model picking (and,
#                        on the Compose side, local-vs-cloud) via
#                        scripts/lib/host-model-picker.sh -- this
#                        script doesn't duplicate that, just gets you
#                        there with one less thing to remember.
# Plus a fourth, non-action entry:
#   - View logs       -> browse and open a past action's saved log.
#
# Every action's output streams completely normally (full terminal,
# unmodified scrolling -- no split-region/scroll-region emulation,
# which behaves inconsistently across terminal emulators) while
# simultaneously being captured to results/logs/. When the action
# finishes, the menu takes the screen back. "View logs" is how you
# revisit that output afterward -- opens the picked file in `less`,
# real native scrolling, not a custom pager.
#
# Deliberately does NOT offer a "custom test" option (e.g. for the
# Soft Thinking / Cold-Stop-over-API research work) -- there is no
# existing integration point for an alternate test suite in
# run_eval_client.py, and this repo's own convention is no stubs or
# placeholders. That needs real design first, not a menu entry that
# does nothing.
#
# All menus use scripts/lib/host-model-picker.sh's host_arrow_menu --
# same host-side, no-curses, no-Docker picker used everywhere else in
# this repo. Requires a real terminal; run it directly, not from a
# script or CI.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=/dev/null
source scripts/lib/host-model-picker.sh

if [ ! -t 0 ]; then
  echo "error: harness-control.sh needs a real terminal -- run it directly, not piped or from a script." >&2
  exit 1
fi

readonly LOG_DIR="results/logs"

pick_backend() {
  host_arrow_menu "Which backend?" "Terraform" "Docker Compose"
}

# run_logged <label> <command...>
# Runs a command with its combined stdout+stderr streamed completely
# normally to the terminal (real scrolling, nothing emulated) while
# simultaneously saving a copy to results/logs/<timestamp>-<label>.log.
# Uses PIPESTATUS[0] rather than plain $? -- under `set -o pipefail`
# the pipeline's own exit status would already reflect the command's
# failure correctly in most cases, but PIPESTATUS is unambiguous
# regardless of pipefail state and is the standard, correct way to
# recover a specific stage's exit code from a pipeline.
run_logged() {
  local label="$1"; shift
  mkdir -p "${LOG_DIR}"
  local log_file
  log_file="${LOG_DIR}/$(date +%Y%m%d-%H%M%S)-${label}.log"
  echo "--- ${label} (logging to ${log_file}) ---"
  "$@" 2>&1 | tee "${log_file}"
  local rc=${PIPESTATUS[0]}
  echo "--- ${label} finished (exit ${rc}) -- log saved to ${log_file} ---"
  return "${rc}"
}

deploy() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) run_logged "deploy-terraform" make tf-apply ;;
    "Docker Compose") run_logged "deploy-compose" docker-compose up -d server ;;
  esac
}

remove() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) run_logged "remove-terraform" make tf-destroy ;;
    "Docker Compose") run_logged "remove-compose" docker-compose down ;;
  esac
}

run_eval() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) run_logged "eval-terraform" bash scripts/tf-select-and-run-eval.sh ;;
    "Docker Compose") run_logged "eval-compose" bash scripts/select-and-run-eval.sh ;;
  esac
}

view_logs() {
  local -a logs=()
  if [ -d "${LOG_DIR}" ]; then
    while IFS= read -r f; do
      logs+=("$f")
    done < <(find "${LOG_DIR}" -maxdepth 1 -type f -name '*.log' | sort -r)
  fi
  if [ "${#logs[@]}" -eq 0 ]; then
    echo "No logs yet -- run something first." >&2
    return 1
  fi
  local selected
  selected="$(host_arrow_menu "Pick a log to view (newest first):" "${logs[@]}")" || {
    echo "Cancelled." >&2
    return 1
  }
  less "${selected}"
}

while true; do
  action="$(host_arrow_menu \
    "=== opencode-model-eval control ===" \
    "Deploy harness" "Remove harness" "Run an eval" "View logs" "Quit")" || exit 0

  case "$action" in
    "Deploy harness") deploy || echo "Deploy failed -- see output above." >&2 ;;
    "Remove harness") remove || echo "Remove failed -- see output above." >&2 ;;
    "Run an eval") run_eval || echo "Eval run failed or was cancelled -- see output above." >&2 ;;
    "View logs") view_logs ;;
    Quit) exit 0 ;;
  esac
done
