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
# Four modes:
#   - provider/id given directly (e.g. opencode/hy3-free): skips
#     discovery entirely, same as discover_and_select_model.py's own
#     --model flag.
#   - --list-json: fetch the candidate list, print it, exit -- no
#     selection, no eval-client run. For a caller (e.g.
#     harness-control.sh) that wants to do the picking itself in its
#     own context rather than letting this script prompt from
#     wherever it happens to be invoked (e.g. a tmux pane other than
#     the one showing the menu).
#   - No model given, real terminal (`[ -t 0 ]`): the discover
#     container runs non-interactively (--list-json, no -it, no TTY
#     ever touches Docker) to fetch the live candidate list, then
#     scripts/lib/host-model-picker.sh's arrow-key menu runs entirely
#     on the HOST to pick one. Picking is a host-terminal concern, not
#     something that belongs inside a container -- an earlier attempt
#     put an arrow-key menu inside the container via Python's curses
#     module (git history: "feat: ncurses arrow-key menu for the
#     discovery picker") -- never merged, superseded by this design.
#   - No model given, no real terminal (CI, piped input): the discover
#     container auto-selects via its own size heuristic, same as
#     before.
#
# Usage:
#   bash scripts/tf-select-and-run-eval.sh                       # live discovery (prompts on a real terminal)
#   bash scripts/tf-select-and-run-eval.sh opencode/hy3-free       # direct, no discovery
#   bash scripts/tf-select-and-run-eval.sh --dry-run opencode/hy3-free  # print, don't run
#   bash scripts/tf-select-and-run-eval.sh --list-json              # candidates only, no run
#
# Requires: `terraform apply` already run at least once (server image
# built, network + volume created) -- this script runs plain `docker`
# against those Terraform-managed resources by their fixed names
# (terraform/main.tf's docker_network.eval_net.name and
# docker_image.harness's name are both static literals, not computed
# values, so no `terraform output` round-trip is needed to find them).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/lib/host-model-picker.sh

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
list_json=false
direct_model=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    --list-json) list_json=true ;;
    *) direct_model="$arg" ;;
  esac
done

run() {
  echo "$*"
  if [ "${dry_run}" = false ]; then
    "$@"
  fi
}

discover_out_dir="${HARNESS_ROOT}/results/discovered"
mkdir -p "${discover_out_dir}"

# Base discover invocation shared by every non-direct-model path --
# never -it, never touches a TTY. Extra args (--list-json / bare
# auto-select) are appended by each caller below.
discover_base_cmd=(docker run --rm
  --entrypoint python3
  -v "${HARNESS_ROOT}/auth-data/auth.json:/home/harness/.local/share/opencode/auth.json:ro"
  -v "${discover_out_dir}:/results"
  # Read-write, not read-only: `opencode models --verbose` here is a
  # raw CLI invocation (not a request to the persistent server), and
  # the opencode CLI writes its own log file on ANY invocation, serve
  # or not. This is the exact same bug already fixed on
  # docker_container.discover in terraform/main.tf -- but that
  # Terraform resource is never actually used by this script (it only
  # borrows the image/network/volume NAMES Terraform created, via
  # plain `docker run`, not `docker start` on that resource), so
  # fixing the .tf file alone left this independent hardcoded mount
  # unfixed. Confirmed live: `terraform apply` correctly showed no
  # drift after the .tf fix (nothing about that resource is actually
  # exercised here), and the exact same "Unknown: FileSystem.open"
  # error reproduced from this line regardless.
  -v "${LOG_VOLUME}:/home/harness/.local/share/opencode/log"
  "${IMAGE}"
  /usr/local/bin/discover_and_select_model.py
)

# --list-json: fetch the candidate list and exit -- no selection, no
# eval-client run. For a caller (e.g. harness-control.sh) that wants
# to do the actual picking itself, in its own context, rather than
# letting this script's own resolve-and-run flow prompt interactively
# from wherever this script happens to be invoked. Reuses
# discover_base_cmd rather than re-deriving the same image/network/
# volume names a second time elsewhere.
if [ "${list_json}" = true ]; then
  exec "${discover_base_cmd[@]}" --list-json
fi

# --- Step 1: resolve provider/model -------------------------------------
if [ -n "${direct_model}" ]; then
  if [[ "${direct_model}" != */* ]]; then
    echo "error: model must be provider/id (e.g. opencode/hy3-free), got: ${direct_model}" >&2
    exit 1
  fi
  provider="${direct_model%%/*}"
  model_id="${direct_model#*/}"
  echo "Using directly (no discovery): ${provider}/${model_id}"
elif [ -t 0 ]; then
  echo "No model given, real terminal detected -- fetching live candidates..."
  list_cmd=("${discover_base_cmd[@]}" --list-json)

  if [ "${dry_run}" = true ]; then
    echo "${list_cmd[*]}"
    echo "(dry-run: cannot pick a model without actually fetching the candidate list -- stopping here)"
    exit 0
  fi

  candidates_json="$("${list_cmd[@]}")"
  selected_full_id="$(host_model_picker "${candidates_json}")" || {
    echo "No model selected." >&2
    exit 1
  }
  provider="${selected_full_id%%/*}"
  model_id="${selected_full_id#*/}"
  echo "Selected: ${provider}/${model_id}"
else
  echo "No model given, no real terminal -- running unattended auto-select..."
  auto_cmd=("${discover_base_cmd[@]}")

  if [ "${dry_run}" = true ]; then
    echo "${auto_cmd[*]}"
    echo "(dry-run: cannot resolve a model without actually running discovery -- stopping here)"
    exit 0
  fi

  "${auto_cmd[@]}"

  env_file="${discover_out_dir}/discovered-model.env"
  if [ ! -f "${env_file}" ]; then
    echo "error: discovery ran but ${env_file} wasn't written -- check the output above" >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  source "${env_file}"
  provider="${OPENCODE_MODEL_PROVIDER:?discovery did not set OPENCODE_MODEL_PROVIDER}"
  model_id="${OPENCODE_MODEL_ID:?discovery did not set OPENCODE_MODEL_ID}"
  echo "Selected via discovery (auto): ${provider}/${model_id}"
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
