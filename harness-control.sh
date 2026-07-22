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
#   - Run an eval     -> provider/model (and, on the Compose side,
#                        local-vs-cloud) picking happens RIGHT HERE,
#                        in this menu pane, via
#                        scripts/lib/host-model-picker.sh's
#                        host_arrow_menu -- then the resolved model is
#                        passed directly to scripts/tf-select-and-run-eval.sh
#                        or scripts/select-and-run-eval.sh (both accept
#                        a direct provider/id argument that skips their
#                        own discovery/picker entirely). Deliberately
#                        NOT delegated to those scripts' own interactive
#                        pickers: since the actual eval run happens in
#                        the OUTPUT pane (tmux send-keys), a nested
#                        picker there would render somewhere other than
#                        the menu pane, needing a pane switch just to
#                        use it -- confirmed live as real friction with
#                        an earlier version of this design.
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
#
# Switches pane FOCUS to the output pane before sending the command,
# and back to the menu pane once it completes -- fixes real friction
# reported live: without this, interacting with something that needs
# keyboard input in the output pane (scrolling/quitting `less`, or
# terraform apply's own "Enter a value:" confirmation prompt when
# AUTO_APPROVE isn't set) required manually clicking over to that pane
# first, every single time.
run_in_output_pane() {
  local inner="$1"
  tmux select-pane -t "${OUTPUT_PANE}"
  tmux send-keys -t "${OUTPUT_PANE}" "bash -c $(printf '%q' "${inner}")" C-m
  tmux wait-for "${WAIT_CHANNEL}"
  tmux select-pane -t "${SESSION}:0.0"
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
  backend="$(pick_backend)" || return 1
  case "$backend" in
    Terraform) run_logged "deploy-terraform" make tf-apply ;;
    "Docker Compose") run_logged "deploy-compose" docker-compose up -d server ;;
  esac
}

remove() {
  local backend
  backend="$(pick_backend)" || return 1
  case "$backend" in
    Terraform) run_logged "remove-terraform" make tf-destroy ;;
    "Docker Compose") run_logged "remove-compose" docker-compose down ;;
  esac
}

# jupyter_up / jupyter_down -- item 9's start/stop control surface.
# Mirrors deploy/remove's pick_backend pattern rather than the one-shot
# run_logged calls used for eval/discover, since Jupyter is meant to
# stay running across an authoring session like `server`, not exit
# immediately.
jupyter_up() {
  local backend
  backend="$(pick_backend)" || return 1
  case "$backend" in
    Terraform) run_logged "jupyter-up-terraform" make tf-jupyter-up ;;
    "Docker Compose") run_logged "jupyter-up-compose" make jupyter-up ;;
  esac
}

jupyter_down() {
  local backend
  backend="$(pick_backend)" || return 1
  case "$backend" in
    Terraform) run_logged "jupyter-down-terraform" make tf-jupyter-down ;;
    "Docker Compose") run_logged "jupyter-down-compose" make jupyter-down ;;
  esac
}

# pick_cloud_model_terraform / pick_cloud_model_compose
# Fetch the candidate list directly (not via run_in_output_pane --
# this is a quick JSON fetch, not something worth streaming/logging)
# and run host_model_picker right here in the menu pane. Prints
# "provider/id" on success; returns 1 (no output) if the fetch fails
# or the picker is cancelled. This is the whole point of this
# rewrite: picking never happens inside a command injected into the
# output pane, so there's never a nested interactive prompt rendering
# somewhere the menu pane isn't -- confirmed live as real friction
# with the previous design (tf-select-and-run-eval.sh's own picker
# rendering in the output pane, requiring a pane switch to use it).
pick_cloud_model_terraform() {
  local candidates_json
  candidates_json="$(bash scripts/tf-select-and-run-eval.sh --list-json)" || {
    echo "Failed to fetch candidates." >&2
    return 1
  }
  host_model_picker "${candidates_json}"
}

pick_cloud_model_compose() {
  local candidates_json
  candidates_json="$(docker-compose run --rm -T discover --list-json)" || {
    echo "Failed to fetch candidates." >&2
    return 1
  }
  host_model_picker "${candidates_json}"
}

run_eval_terraform() {
  # tf-select-and-run-eval.sh only ever does cloud models -- local
  # Ollama models on the Terraform path are separate
  # docker_container.local_ollama resources (see terraform/main.tf),
  # not something this script runs an eval against.
  local picked
  picked="$(pick_cloud_model_terraform)" || return 1
  run_logged "eval-terraform" bash scripts/tf-select-and-run-eval.sh "${picked}"
}

run_eval_compose() {
  # Mirrors select-and-run-eval.sh's own top-level menu (cloud vs each
  # local service), but built and shown HERE instead of letting that
  # script show it -- same reasoning as pick_cloud_model_terraform.
  local -a options=("cloud")
  local svc
  while IFS= read -r svc; do
    [ -z "${svc}" ] && continue
    options+=("${svc}")
  done < <(docker-compose config --services 2>/dev/null | grep -v -E '^(server|discover|eval)$' || true)

  local picked_target
  picked_target="$(host_arrow_menu "Which model?" "${options[@]}")" || return 1

  if [ "${picked_target}" = "cloud" ]; then
    local picked
    picked="$(pick_cloud_model_compose)" || return 1
    run_logged "eval-compose" bash scripts/select-and-run-eval.sh "${picked}"
  else
    # Local service name -- already fully non-interactive (no
    # discovery, no picker involved), runs directly.
    run_logged "eval-compose" bash scripts/select-and-run-eval.sh "${picked_target}"
  fi
}

run_eval() {
  local backend
  backend="$(pick_backend)" || return 1
  case "$backend" in
    Terraform) run_eval_terraform ;;
    "Docker Compose") run_eval_compose ;;
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
  selected="$(host_arrow_menu "Pick a log to view (newest first):" "${logs[@]}")" || return 1
  run_in_output_pane \
    "less -R $(printf '%q' "${selected}"); tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"
}

# browse_results -- distinct from view_logs: browses actual evaluation
# RESULTS (results/<model_slug>/report.json + per-category CVV output),
# not action/session logs. Two levels: pick a model, then pick either
# its report.json summary or drill into one category's raw output
# files. Read-only, same `less -R` open pattern as view_logs.
# browse_results -- distinct from view_logs: browses actual evaluation
# RESULTS (report.json + per-category CVV output), not action/session
# logs. Two levels: pick a model, then pick either its report.json
# summary or drill into one category's raw output files. Read-only,
# same `less -R` open pattern as view_logs.
browse_results() {
  # Models are derived from wherever report.json actually lives, not
  # from a fixed depth under results/ -- confirmed live that this
  # varies by backend: results/<model_slug>/report.json (Compose/cloud
  # eval, flat) vs results/local/<model_slug>/report.json (Terraform's
  # local-model containers, one level deeper). A maxdepth-1 listing
  # instead picked up results/local and results/discovered themselves
  # (no report.json in either) as if they were models, landing on a
  # silent dead end when picked.
  local -a model_dirs=()
  if [ -d results ]; then
    while IFS= read -r report; do
      model_dirs+=("$(dirname "$report")")
    done < <(find results -mindepth 2 -name report.json | sort)
  fi
  if [ "${#model_dirs[@]}" -eq 0 ]; then
    run_in_output_pane \
      "echo 'No results yet -- run an eval first.'; tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"
    return 1
  fi

  local -a display=()
  local d
  for d in "${model_dirs[@]}"; do
    display+=("${d#results/}")
  done

  local picked_rel
  picked_rel="$(host_arrow_menu "Pick a model's results:" "${display[@]}")" || return 1
  local model_dir="results/${picked_rel}"

  local report="${model_dir}/report.json"
  local -a options=("report.json (summary)")
  local -a categories=()
  local c
  while IFS= read -r c; do
    # Skip categories with no files at all -- e.g. a category that hit
    # quota-exhaustion on tier 1 and never got far enough to write a
    # tier file. Offering these as options was itself a dead end
    # (confirmed live from a real run where every category bailed out
    # on tier 1's quota check before writing anything).
    if [ -n "$(find "$c" -maxdepth 1 -type f -print -quit)" ]; then
      categories+=("$(basename "$c")")
      options+=("category: $(basename "$c")")
    fi
  done < <(find "${model_dir}" -mindepth 1 -maxdepth 1 -type d | sort)

  local picked
  picked="$(host_arrow_menu "${model_dir} -- what to view?" "${options[@]}")" || return 1

  if [ "${picked}" = "report.json (summary)" ]; then
    run_in_output_pane \
      "if command -v jq >/dev/null 2>&1; then jq . $(printf '%q' "${report}") | less -R; else less -R $(printf '%q' "${report}"); fi; tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"
    return
  fi

  local cat_name="${picked#category: }"
  local -a files=()
  while IFS= read -r f; do
    files+=("$f")
  done < <(find "${model_dir}/${cat_name}" -maxdepth 1 -type f | sort)
  local selected_file
  selected_file="$(host_arrow_menu "${model_dir}/${cat_name} -- pick a file:" "${files[@]}")" || return 1
  run_in_output_pane \
    "less -R $(printf '%q' "${selected_file}"); tmux wait-for -S $(printf '%q' "${WAIT_CHANNEL}")"
}

while true; do
  action="$(host_arrow_menu \
    "=== opencode-model-eval control ===" \
    "Deploy harness" "Remove harness" "Run an eval" "View logs" "Browse results" \
    "Start Jupyter" "Stop Jupyter" "Quit")" || {
    tmux kill-session -t "${SESSION}" 2>/dev/null || true
    exit 0
  }

  case "$action" in
    "Deploy harness") deploy || echo "Deploy failed -- see output above." >&2 ;;
    "Remove harness") remove || echo "Remove failed -- see output above." >&2 ;;
    "Run an eval") run_eval || echo "Eval run failed or was cancelled -- see output above." >&2 ;;
    "View logs") view_logs || true ;;
    "Browse results") browse_results || true ;;
    "Start Jupyter") jupyter_up || echo "Jupyter start failed -- see output above." >&2 ;;
    "Stop Jupyter") jupyter_down || echo "Jupyter stop failed -- see output above." >&2 ;;
    Quit)
      tmux kill-session -t "${SESSION}" 2>/dev/null || true
      exit 0
      ;;
  esac
done
