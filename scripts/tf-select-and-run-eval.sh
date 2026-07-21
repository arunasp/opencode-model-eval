#!/bin/bash
# tf-select-and-run-eval.sh -- Terraform-infra equivalent of
# scripts/select-and-run-eval.sh's `eval` target, for cloud models.
#
# The Compose path's `eval` service is generic (one image, provider/
# model passed at `docker-compose run` time) -- this repo's Terraform
# path used to instead bake a fixed provider/model matrix into
# var.models / docker_container.eval, one static container per entry.
# That matrix is gone (see terraform/main.tf's docker_container.discover
# comment for why); this script is its replacement, matching the
# Compose path's actual pattern: nothing is provisioned ahead of time
# for "which model to run", it's resolved at invocation time.
#
# Two modes, same distinction docker_container.discover already draws:
#   - No model given: live discovery. Runs the discover container
#     (python3 discover_and_select_model.py, no --model), which queries
#     `opencode models --verbose` for real and auto-selects a free
#     candidate.
#   - provider/id given directly (e.g. opencode/hy3-free): skips
#     discovery, same as discover_and_select_model.py's own --model
#     flag.
#
# Usage:
#   bash scripts/tf-select-and-run-eval.sh                       # live discovery
#   bash scripts/tf-select-and-run-eval.sh opencode/hy3-free       # direct, no discovery
#   bash scripts/tf-select-and-run-eval.sh --dry-run opencode/hy3-free  # print, don't run
#
# Requires: `terraform apply` already run at least once (server image
# built, network + volume created) -- this script runs plain `docker`
# against those Terraform-managed resources by their fixed names
# (terraform/main.tf's docker_network.eval_net.name and
# docker_image.harness's name are both static literals, not computed
# values, so no `terraform output` round-trip is needed to find them).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly IMAGE="opencode-model-eval-harness:latest"
readonly NETWORK="opencode-model-eval-net"
readonly LOG_VOLUME="opencode-model-eval-log"
HARNESS_ROOT="$(pwd)"
readonly HARNESS_ROOT

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker not found on PATH" >&2
  exit 1
fi

dry_run=false
direct_model=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    *) direct_model="$arg" ;;
  esac
done

run() {
  echo "$*"
  if [ "${dry_run}" = false ]; then
    "$@"
  fi
}

# --- Step 1: resolve provider/model -------------------------------------
if [ -n "${direct_model}" ]; then
  if [[ "${direct_model}" != */* ]]; then
    echo "error: model must be provider/id (e.g. opencode/hy3-free), got: ${direct_model}" >&2
    exit 1
  fi
  provider="${direct_model%%/*}"
  model_id="${direct_model#*/}"
  echo "Using directly (no discovery): ${provider}/${model_id}"
else
  echo "No model given -- running live discovery via 'opencode models --verbose'..."
  discover_out_dir="${HARNESS_ROOT}/results/discovered"
  mkdir -p "${discover_out_dir}"

  discover_cmd=(docker run --rm
    --entrypoint python3
    -v "${HARNESS_ROOT}/auth-data/auth.json:/home/harness/.local/share/opencode/auth.json:ro"
    -v "${discover_out_dir}:/results"
    -v "${LOG_VOLUME}:/home/harness/.local/share/opencode/log:ro"
    "${IMAGE}"
    /usr/local/bin/discover_and_select_model.py
  )

  if [ "${dry_run}" = true ]; then
    echo "${discover_cmd[*]}"
    echo "(dry-run: cannot resolve a model without actually running discovery -- stopping here)"
    exit 0
  fi

  "${discover_cmd[@]}"

  env_file="${discover_out_dir}/discovered-model.env"
  if [ ! -f "${env_file}" ]; then
    echo "error: discovery ran but ${env_file} wasn't written -- check the output above" >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  source "${env_file}"
  provider="${OPENCODE_MODEL_PROVIDER:?discovery did not set OPENCODE_MODEL_PROVIDER}"
  model_id="${OPENCODE_MODEL_ID:?discovery did not set OPENCODE_MODEL_ID}"
  echo "Selected via discovery: ${provider}/${model_id}"
fi

# --- Step 2: run the eval-client container against it -------------------
run docker run --rm \
  --network "${NETWORK}" \
  -e "OPENCODE_SERVER_URL=http://server:4096" \
  -e "OPENCODE_MODEL_PROVIDER=${provider}" \
  -e "OPENCODE_MODEL_ID=${model_id}" \
  -v "${HARNESS_ROOT}/task-suite:/task-suite:ro" \
  -v "${HARNESS_ROOT}/auth-data/auth.json:/home/harness/.local/share/opencode/auth.json:ro" \
  -v "${HARNESS_ROOT}/results:/results" \
  -v "${LOG_VOLUME}:/home/harness/.local/share/opencode/log:ro" \
  "${IMAGE}" eval-client
