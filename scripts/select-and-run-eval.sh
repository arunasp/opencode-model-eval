#!/bin/bash
# select-and-run-eval.sh -- opencode /models-style interactive picker
# for this repo's eval targets, wrapping the docker-compose invocations
# documented in the README so you don't have to remember/retype
# `docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=... -e
# OPENCODE_MODEL_ID=... eval` or the per-local-model service name.
#
# Two sources, deliberately handled differently:
#   - Cloud models: live discovery via the `discover` Compose service
#     (queries `opencode models --verbose` for real), run with -T
#     (no pseudo-TTY -- it never needs one) and --list-json (just
#     fetches the candidate list, doesn't pick). Picking itself runs
#     entirely on the HOST via scripts/lib/host-model-picker.sh's
#     arrow-key menu, same one used by
#     scripts/tf-select-and-run-eval.sh on the Terraform side -- an
#     earlier version had `discover` itself prompt via attached stdin
#     inside docker-compose; moved out because picking a model is a
#     host-terminal concern, not something that belongs inside a
#     container.
#   - Local Ollama models: derived LIVE from `docker-compose config
#     --services`, filtered to exclude the 3 non-model services
#     (server/discover/eval) -- this never drifts from the actual
#     configured service list, unlike a hardcoded copy would.
#
# Usage:
#   bash scripts/select-and-run-eval.sh              # interactive menu
#   bash scripts/select-and-run-eval.sh hy3           # direct, by name, no menu
#   bash scripts/select-and-run-eval.sh --dry-run hy3 # print the command, don't run it
#   bash scripts/select-and-run-eval.sh cloud         # go straight to cloud discovery
#   bash scripts/select-and-run-eval.sh opencode/hy3-free  # direct provider/id, no discovery at all
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/lib/host-model-picker.sh

if ! command -v docker-compose >/dev/null 2>&1; then
  echo "error: docker-compose not found on PATH" >&2
  exit 1
fi

LOCAL_SERVICES="$(docker-compose config --services 2>/dev/null | grep -v -E '^(server|discover|eval)$' || true)"

dry_run=false
direct_name=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    *) direct_name="$arg" ;;
  esac
done

# Build the full option list once, regardless of interactive vs direct
# mode -- keeps both paths validating against the exact same set, so
# a name typo in direct mode gets the same clear error an out-of-range
# menu number would. "cloud" is a single synthetic entry standing in
# for live discovery, not one option per model -- discover_and_select_model.py
# itself lists/prompts once run.
names=("cloud")
kinds=("cloud")

while IFS= read -r svc; do
  [ -z "$svc" ] && continue
  names+=("$svc")
  kinds+=("local")
done <<< "$LOCAL_SERVICES"

run_selected() {
  local idx="$1"
  local name="${names[$idx]}"
  local kind="${kinds[$idx]}"

  if [ "$kind" = "cloud" ]; then
    if [ -t 0 ]; then
      echo "docker-compose run --rm -T discover --list-json"
      if [ "$dry_run" = false ]; then
        candidates_json="$(docker-compose run --rm -T discover --list-json)"
        selected_full_id="$(host_model_picker "$candidates_json")" || {
          echo "No model selected." >&2
          exit 1
        }
        provider="${selected_full_id%%/*}"
        model_id="${selected_full_id#*/}"
      fi
    else
      echo "docker-compose run --rm -T discover"
      if [ "$dry_run" = false ]; then
        # No real terminal (CI, scripted) -- no one to answer a host
        # prompt, so let the container fall back to its own unattended
        # auto-select instead, writing results/discovered-model.env.
        docker-compose run --rm -T discover
        env_file="results/discovered/discovered-model.env"
        if [ ! -f "$env_file" ]; then
          echo "error: discovery ran but $env_file wasn't written -- check the output above" >&2
          exit 1
        fi
        # shellcheck source=/dev/null
        source "$env_file"
        provider="${OPENCODE_MODEL_PROVIDER:?discovery did not set OPENCODE_MODEL_PROVIDER}"
        model_id="${OPENCODE_MODEL_ID:?discovery did not set OPENCODE_MODEL_ID}"
      fi
    fi
    if [ "$dry_run" = false ]; then
      echo "Selected: ${provider}/${model_id}"
      echo "docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
      exec docker-compose run --rm -e OPENCODE_MODEL_PROVIDER="$provider" -e OPENCODE_MODEL_ID="$model_id" eval
    fi
  else
    echo "docker-compose run --rm $name"
    if [ "$dry_run" = false ]; then
      exec docker-compose run --rm "$name"
    fi
  fi
}

if [ -n "$direct_name" ]; then
  # provider/id shape (contains a /) -- direct model, no discovery at
  # all. Matches tf-select-and-run-eval.sh's identical convention
  # exactly, so a caller that already resolved a model itself (e.g.
  # harness-control.sh, picking in its own pane rather than letting
  # this script prompt from wherever it's invoked) can skip straight
  # to running it here too. Local service names never contain a /, so
  # there's no ambiguity with the names-array lookup below.
  if [[ "$direct_name" == */* ]]; then
    provider="${direct_name%%/*}"
    model_id="${direct_name#*/}"
    echo "docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
    if [ "$dry_run" = false ]; then
      exec docker-compose run --rm -e OPENCODE_MODEL_PROVIDER="$provider" -e OPENCODE_MODEL_ID="$model_id" eval
    fi
    exit 0
  fi
  for i in "${!names[@]}"; do
    if [ "${names[$i]}" = "$direct_name" ]; then
      run_selected "$i"
      exit 0
    fi
  done
  echo "error: '$direct_name' isn't a known target. Available:" >&2
  printf '  %s\n' "${names[@]}" >&2
  exit 1
fi

echo "=== opencode-model-eval: pick a model to test ==="
echo
echo "Cloud (live discovery -- prompts on this host for the actual model):"
for i in "${!names[@]}"; do
  if [ "${kinds[$i]}" = "cloud" ]; then
    printf "  %2d) %s\n" "$((i+1))" "${names[$i]}"
  fi
done
echo
echo "Local (Ollama, live from docker-compose):"
for i in "${!names[@]}"; do
  if [ "${kinds[$i]}" = "local" ]; then
    printf "  %2d) %s\n" "$((i+1))" "${names[$i]}"
  fi
done
echo
read -rp "Select a number: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#names[@]}" ]; then
  echo "error: invalid selection '$choice'" >&2
  exit 1
fi

run_selected "$((choice-1))"
