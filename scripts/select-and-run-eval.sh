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
#   - Local Ollama models: read from your real global opencode config
#     (OPENCODE_GLOBAL_CONFIG, provider["local/ollama"]["models"] --
#     the actual source of truth as of the batch-4 migration; this
#     project no longer maintains its own copy) and run through the SAME
#     generic `eval` service as cloud models, via explicit
#     OPENCODE_MODEL_PROVIDER/OPENCODE_MODEL_ID env vars -- not 5
#     dedicated per-model services with their own host networking
#     (that design used to exist here and on the Terraform side; on
#     Terraform it caused a real bug -- see terraform/main.tf's
#     docker_container.local_ollama removal comment -- and even here,
#     where that specific bug didn't apply, it was 5 near-duplicate
#     services for no benefit once the generic eval service already
#     does the same job via env vars).
#
# Usage:
#   bash scripts/select-and-run-eval.sh              # interactive menu
#   bash scripts/select-and-run-eval.sh hy3           # direct, by name, no menu
#   bash scripts/select-and-run-eval.sh --dry-run hy3 # print the command, don't run it
#   bash scripts/select-and-run-eval.sh cloud         # go straight to cloud discovery
#   bash scripts/select-and-run-eval.sh opencode/hy3-free  # direct provider/id, no discovery at all
set -euo pipefail

# shellcheck source=/dev/null
source scripts/lib/opencode-global-config.sh

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/lib/host-model-picker.sh
# shellcheck source=/dev/null
source scripts/lib/compose.sh

compose_resolve || exit 1

if [ -z "${OPENCODE_GLOBAL_CONFIG:-}" ]; then
  echo "error: OPENCODE_GLOBAL_CONFIG is empty -- scripts/lib/opencode-global-config.sh should have defaulted it, this shouldn't happen" >&2
  exit 1
fi

# Discover live Ollama models and merge them with your global config
# before reading, so a model you've `ollama pull`ed but haven't
# declared in provider["local/ollama"]["models"] yet still shows up --
# same additive discover_local_ollama_models.py mechanism the
# container already runs at startup, just run here on the host first,
# before the picker itself. Ollama unreachable degrades gracefully
# (confirmed in discover_local_ollama_models.py: falls back to the
# global config's own declared list unchanged, doesn't fail the picker).
MERGED_LOCAL_CONFIG="$(mktemp)"
trap 'rm -f "${MERGED_LOCAL_CONFIG}"' EXIT
python3 scripts/discover_local_ollama_models.py \
  --base-config "${OPENCODE_GLOBAL_CONFIG}" \
  --ollama-tags-url "${OPENCODE_OLLAMA_TAGS_URL:-http://localhost:11434/api/tags}" \
  --output "${MERGED_LOCAL_CONFIG}" \
  --provider-key local/ollama \
  --timeout 3
LOCAL_MODELS="$(python3 scripts/tools/read_local_ollama_models.py --config "${MERGED_LOCAL_CONFIG}")"

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

while IFS= read -r model; do
  [ -z "$model" ] && continue
  names+=("$model")
  kinds+=("local")
done <<< "$LOCAL_MODELS"

# shellcheck source=/dev/null
source scripts/lib/server-lifecycle.sh

# `docker-compose run --rm eval` implicitly starts `server` via
# Compose's own depends_on -- if something else (confirmed live:
# Terraform's server container, back when it shared the same default
# port as Compose) already holds OPENCODE_SERVE_PORT, that implicit
# start hits the exact same "port is already allocated" bind failure
# harness-control.sh's deploy() now catches, but this script never
# went through deploy() at all -- confirmed live, a real gap this
# closes. Compose (49605) and Terraform (49604) now default to
# different ports specifically so this stops being the common case,
# but the check stays as a safety net for an explicit override.
ensure_no_conflicting_server() {
  local existing
  existing="$(existing_server_backend "${OPENCODE_SERVE_PORT:-49605}")" || return 0  # nothing up -- depends_on will start it cleanly
  if [ "${existing}" = "Docker Compose" ]; then
    return 0  # already Compose's own server -- depends_on will just reuse it
  fi
  # Something else holds this SPECIFIC port -- would collide. With
  # Compose (49605) and Terraform (49604) now on different default
  # ports, this only fires if OPENCODE_SERVE_PORT was explicitly
  # overridden back into a collision -- the common case is this
  # simply never triggers anymore.
  if [ -n "${FORCE_REDEPLOY:-}" ]; then
    echo "FORCE_REDEPLOY=1 set -- tearing down existing ${existing} deployment on this port first"
  elif [ -t 0 ]; then
    local choice
    choice="$(host_arrow_menu \
      "A server is already reachable on this port (${existing}), which conflicts with Compose's own server. Tear it down and continue?" \
      "Tear down and continue" "Abort")" || return 1
    [ "${choice}" = "Abort" ] && { echo "Aborted -- resolve the port conflict manually, or set FORCE_REDEPLOY=1." >&2; return 1; }
  else
    echo "error: port conflict with an existing ${existing} deployment, and no TTY to ask -- set FORCE_REDEPLOY=1 to tear it down automatically, or resolve manually." >&2
    return 1
  fi
  case "${existing}" in
    Terraform) make tf-destroy ;;
    *) echo "Can't automatically tear down an unidentified backend -- resolve manually." >&2; return 1 ;;
  esac
}

run_selected() {
  local idx="$1"
  local name="${names[$idx]}"
  local kind="${kinds[$idx]}"

  if [ "$dry_run" = false ]; then
    ensure_images_built
    ensure_no_conflicting_server || exit 1
  fi

  if [ "$kind" = "cloud" ]; then
    if [ -t 0 ]; then
      echo "$(compose_str) run --rm -T discover --list-json"
      if [ "$dry_run" = false ]; then
        candidates_json="$(compose run --rm -T discover --list-json)"
        selected_full_id="$(host_model_picker "$candidates_json")" || {
          echo "No model selected." >&2
          exit 1
        }
        provider="${selected_full_id%%/*}"
        model_id="${selected_full_id#*/}"
      fi
    else
      echo "$(compose_str) run --rm -T discover"
      if [ "$dry_run" = false ]; then
        # No real terminal (CI, scripted) -- no one to answer a host
        # prompt, so let the container fall back to its own unattended
        # auto-select instead, writing results/discovered-model.env.
        compose run --rm -T discover
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
      echo "$(compose_str) run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
      exec "${COMPOSE[@]}" run --rm -e OPENCODE_MODEL_PROVIDER="$provider" -e OPENCODE_MODEL_ID="$model_id" eval
    fi
  else
    echo "$(compose_str) run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=$name eval"
    if [ "$dry_run" = false ]; then
      exec "${COMPOSE[@]}" run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID="$name" eval
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
  if [[ "$direct_name" == local/ollama/* ]]; then
    provider="local/ollama"
    model_id="${direct_name#local/ollama/}"
    echo "$(compose_str) run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
    if [ "$dry_run" = false ]; then
      exec "${COMPOSE[@]}" run --rm -e OPENCODE_MODEL_PROVIDER="$provider" -e OPENCODE_MODEL_ID="$model_id" eval
    fi
    exit 0
  fi
  if [[ "$direct_name" == */* ]]; then
    provider="${direct_name%%/*}"
    model_id="${direct_name#*/}"
    echo "$(compose_str) run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
    if [ "$dry_run" = false ]; then
      exec "${COMPOSE[@]}" run --rm -e OPENCODE_MODEL_PROVIDER="$provider" -e OPENCODE_MODEL_ID="$model_id" eval
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
echo "Local (Ollama, from your global opencode config):"
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
