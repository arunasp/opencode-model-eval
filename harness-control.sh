#!/bin/bash
# harness-control.sh -- top-level control UI for opencode-model-eval.
# Run from the repo root: bash harness-control.sh
#
# Persistent 30/70 split, menu left / streaming output right, backed by
# tmux (a hard dependency, no fallback -- settled after actually
# researching how real ncurses-based IDEs do this: tvision, the
# library behind the Free Pascal IDE's look, maintains its own full
# virtual screen buffer with diff-based redraw just to get this --
# ~500 lines of buffer/flush logic alone. tmux already solves the same
# problem correctly and portably as a mature, near-universal binary;
# reimplementing that machinery here would be real over-engineering
# for what this script needs).
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
#   - View logs       -> browse and open a past action's saved log,
#                        opened in `less` in the output pane.
#
# How the split works: this script bootstraps a tmux session with two
# panes (left 30% menu, right 70% output) on first run, then re-execs
# itself INSIDE the left pane with HARNESS_CONTROL_TMUX_PANE set as a
# sentinel so it knows not to bootstrap again. Every action's command
# is injected into the right pane via `tmux send-keys` -- tmux itself
# does all the real compositing and independent per-pane scrolling,
# nothing here does. `tmux wait-for` (a real synchronization
# primitive, not a polling loop) blocks the menu pane until the output
# pane's command signals completion, so results/logs and exit-code
# reporting still work exactly as before.
#
# Deliberately does NOT offer a "custom test" option (e.g. for the
# Soft Thinking / Cold-Stop-over-API research work) -- there is no
# existing integration point for an alternate test suite in
# run_eval_client.py, and this repo's own convention is no stubs or
# placeholders. That needs real design first, not a menu entry that
# does nothing.
#
# All menus use scripts/lib/host-model-picker.sh's host_arrow_menu --
# same host-side, no-curses picker used everywhere else in this repo.
# Requires a real terminal and tmux; run it directly, not from a
# script or CI.
set -euo pipefail

# Resolve an absolute path to this script BEFORE cd'ing, so the
# tmux-bootstrap re-exec below can find it regardless of how it was
# originally invoked (relative path, PATH lookup, etc.).
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
readonly SCRIPT_PATH

cd "$(dirname "${SCRIPT_PATH}")"

if ! command -v tmux >/dev/null 2>&1; then
  echo "error: tmux is required (not found on PATH). Install it -- e.g. 'apt install tmux' -- and retry." >&2
  exit 1
fi

if [ ! -t 0 ]; then
  echo "error: harness-control.sh needs a real terminal -- run it directly, not piped or from a script." >&2
  exit 1
fi

# --- Bootstrap: create the tmux session and split, then hand off ---------
if [ -z "${HARNESS_CONTROL_TMUX_PANE:-}" ]; then
  session="harness-control-$$"
  cols="$(tput cols)"
  lines="$(tput lines)"
  tmux new-session -d -s "${session}" -x "${cols}" -y "${lines}"
  tmux set-option -t "${session}" -g mouse on
  # Split off the right 70% as the output pane; pane 0 (left, 30%)
  # stays the menu. -l (absolute cell count) rather than -p
  # (percentage): -p reproducibly fails with tmux's own "size missing"
  # error on a freshly created, not-yet-attached session in this
  # environment -- confirmed live, -l does not have that problem.
  output_width=$(( cols * 70 / 100 ))
  tmux split-window -h -l "${output_width}" -t "${session}:0.0"
  output_pane="${session}:0.1"
  tmux select-pane -t "${session}:0.0"
  tmux send-keys -t "${session}:0.0" \
    "HARNESS_CONTROL_TMUX_PANE=1 HARNESS_CONTROL_SESSION=$(printf '%q' "${session}") HARNESS_CONTROL_OUTPUT_PANE=$(printf '%q' "${output_pane}") $(printf '%q' "${SCRIPT_PATH}")" \
    C-m
  exec tmux attach-session -t "${session}"
fi

# --- Menu-pane mode: HARNESS_CONTROL_TMUX_PANE is set --------------------
readonly SESSION="${HARNESS_CONTROL_SESSION:?}"
readonly OUTPUT_PANE="${HARNESS_CONTROL_OUTPUT_PANE:?}"
readonly WAIT_CHANNEL="harness-control-action-done-$$"

# shellcheck source=/dev/null
source scripts/lib/host-model-picker.sh

readonly LOG_DIR="results/logs"

pick_backend() {
  host_arrow_menu "Which backend?" "Terraform" "Docker Compose"
}

# run_in_output_pane <bash-command-string>
# Sends a command to the output pane via tmux send-keys, then blocks
# on `tmux wait-for` (a real synchronization primitive tmux provides
# for exactly this -- not a polling loop) until the injected command
# signals completion. The command string is responsible for signaling
# `tmux wait-for -S "$WAIT_CHANNEL"` itself once done.
run_in_output_pane() {
  local inner="$1"
  tmux send-keys -t "${OUTPUT_PANE}" "bash -c $(printf '%q' "${inner}")" C-m
  tmux wait-for "${WAIT_CHANNEL}"
}

# run_logged <label> <command...>
# Runs a command IN THE OUTPUT PANE, streaming completely normally
# there (tmux gives it a real pty with real native scrolling -- no
# emulation on our end) while saving a copy to
# results/logs/<timestamp>-<label>.log. Recovers the command's real
# exit code (not tee's) via PIPESTATUS[0], written to a temp file the
# menu pane reads back after wait-for unblocks.
run_logged() {
  local label="$1"; shift
  mkdir -p "${LOG_DIR}"
  local log_file
  log_file="${LOG_DIR}/$(date +%Y%m%d-%H%M%S)-${label}.log"
  local rc_file
  rc_file="$(mktemp)"

  local quoted_cmd
  quoted_cmd="$(printf '%q ' "$@")"

  run_in_output_pane \
    "echo '--- ${label} (logging to ${log_file}) ---'; ${quoted_cmd}2>&1 | tee $(printf '%q' "${log_file}"); rc=\${PIPESTATUS[0]}; echo \"--- ${label} finished (exit \${rc}) -- log saved to ${log_file} ---\"; echo \"\${rc}\" > $(printf '%q' "${rc_file}"); tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"

  local rc
  rc="$(cat "${rc_file}")"
  rm -f "${rc_file}"
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
  run_in_output_pane \
    "less $(printf '%q' "${selected}"); tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"
}

while true; do
  action="$(host_arrow_menu \
    "=== opencode-model-eval control ===" \
    "Deploy harness" "Remove harness" "Run an eval" "View logs" "Quit")" || {
    tmux kill-session -t "${SESSION}" 2>/dev/null || true
    exit 0
  }

  case "$action" in
    "Deploy harness") deploy || echo "Deploy failed -- see output above." >&2 ;;
    "Remove harness") remove || echo "Remove failed -- see output above." >&2 ;;
    "Run an eval") run_eval || echo "Eval run failed or was cancelled -- see output above." >&2 ;;
    "View logs") view_logs ;;
    Quit)
      tmux kill-session -t "${SESSION}" 2>/dev/null || true
      exit 0
      ;;
  esac
done
