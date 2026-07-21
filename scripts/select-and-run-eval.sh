#!/bin/bash
# select-and-run-eval.sh -- opencode /models-style interactive picker
# for this repo's eval targets, wrapping the docker-compose invocations
# documented in the README so you don't have to remember/retype
# `docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=... -e
# OPENCODE_MODEL_ID=... eval` or the per-local-model service name.
#
# Two sources, deliberately handled differently:
#   - Cloud models: a small, fixed, rarely-changing set -- mirrored
#     here from terraform/variables.tf's var.models default. Keep
#     these two in sync manually if you add/change a cloud model;
#     not worth adding an HCL parser dependency for 3 known entries.
#   - Local Ollama models: derived LIVE from `docker-compose config
#     --services`, filtered to exclude the 3 non-model services
#     (server/discover/eval) -- this never drifts from the actual
#     configured service list, unlike a hardcoded copy would.
#
# Usage:
#   bash scripts/select-and-run-eval.sh              # interactive menu
#   bash scripts/select-and-run-eval.sh hy3           # direct, by name, no menu
#   bash scripts/select-and-run-eval.sh --dry-run hy3 # print the command, don't run it
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker-compose >/dev/null 2>&1; then
  echo "error: docker-compose not found on PATH" >&2
  exit 1
fi

# name|provider|model_id -- keep in sync with terraform/variables.tf's
# var.models default block.
CLOUD_MODELS=(
  "hy3|opencode|hy3-free"
  "deepseek-v4-pro|deepseek|deepseek-v4-pro"
  "glm-5-2|zhipu|glm-5.2"
)

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
# menu number would.
names=()
providers=()
model_ids=()
kinds=()  # "cloud" or "local"

for entry in "${CLOUD_MODELS[@]}"; do
  IFS='|' read -r name provider model_id <<< "$entry"
  names+=("$name")
  providers+=("$provider")
  model_ids+=("$model_id")
  kinds+=("cloud")
done

while IFS= read -r svc; do
  [ -z "$svc" ] && continue
  names+=("$svc")
  providers+=("")
  model_ids+=("")
  kinds+=("local")
done <<< "$LOCAL_SERVICES"

run_selected() {
  local idx="$1"
  local name="${names[$idx]}"
  local kind="${kinds[$idx]}"

  if [ "$kind" = "cloud" ]; then
    local provider="${providers[$idx]}"
    local model_id="${model_ids[$idx]}"
    echo "docker-compose run --rm -e OPENCODE_MODEL_PROVIDER=$provider -e OPENCODE_MODEL_ID=$model_id eval"
    if [ "$dry_run" = false ]; then
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
echo "Cloud (via var.models):"
for i in "${!names[@]}"; do
  if [ "${kinds[$i]}" = "cloud" ]; then
    printf "  %2d) %-20s provider=%s model=%s\n" "$((i+1))" "${names[$i]}" "${providers[$i]}" "${model_ids[$i]}"
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
