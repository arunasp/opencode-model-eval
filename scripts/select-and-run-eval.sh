#!/bin/bash
# select-and-run-eval.sh -- opencode /models-style interactive picker
# for this repo's eval targets, wrapping the docker-compose invocations
# documented in the README so you don't have to remember/retype
# `docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=... -e
# OPENCODE_MODEL_ID=... eval` or the per-local-model service name.
#
# Two sources, deliberately handled differently:
#   - Cloud models: live discovery via the `discover` Compose service
#     (queries `opencode models --verbose` for real). This used to be a
#     hardcoded CLOUD_MODELS array mirroring terraform/variables.tf's
#     var.models -- that variable is gone now (see terraform/main.tf's
#     docker_container.discover comment: the whole static matrix was
#     removed in favor of live discovery), so a hardcoded copy here
#     would just be a second, now-orphaned source of the same staleness
#     problem. `discover` itself prompts interactively when run with a
#     real terminal attached (docker-compose run keeps stdin attached
#     by default, unlike `run --rm -T`), same picker used on the
#     Terraform side via scripts/tf-select-and-run-eval.sh.
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
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

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
    echo "docker-compose run --rm discover"
    if [ "$dry_run" = false ]; then
      # discover writes results/discovered-model.env; read it back to
      # drive the actual eval run, same handoff tf-select-and-run-eval.sh
      # uses on the Terraform side.
      docker-compose run --rm discover
      env_file="results/discovered/discovered-model.env"
      if [ ! -f "$env_file" ]; then
        echo "error: discovery ran but $env_file wasn't written -- check the output above" >&2
        exit 1
      fi
      # shellcheck source=/dev/null
      source "$env_file"
      : "${OPENCODE_MODEL_PROVIDER:?discovery did not set OPENCODE_MODEL_PROVIDER}"
      : "${OPENCODE_MODEL_ID:?discovery did not set OPENCODE_MODEL_ID}"
      echo "docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=$OPENCODE_MODEL_PROVIDER -e OPENCODE_MODEL_ID=$OPENCODE_MODEL_ID eval"
      exec docker-compose run --rm -e OPENCODE_MODEL_PROVIDER="$OPENCODE_MODEL_PROVIDER" -e OPENCODE_MODEL_ID="$OPENCODE_MODEL_ID" eval
    fi
  else
    echo "docker-compose run --rm $name"
    if [ "$dry_run" = false ]; then
      exec docker-compose run --rm "$name"
    fi
  fi
}

if [ -n "$direct_name" ]; then
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
echo "Cloud (live discovery -- prompts again for the actual model):"
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
