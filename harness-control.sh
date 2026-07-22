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

pick_backend() {
  host_arrow_menu "Which backend?" "Terraform" "Docker Compose"
}

deploy() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) make tf-apply ;;
    "Docker Compose") docker-compose up -d server ;;
  esac
}

remove() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) make tf-destroy ;;
    "Docker Compose") docker-compose down ;;
  esac
}

run_eval() {
  local backend
  backend="$(pick_backend)" || { echo "Cancelled." >&2; return 1; }
  case "$backend" in
    Terraform) bash scripts/tf-select-and-run-eval.sh ;;
    "Docker Compose") bash scripts/select-and-run-eval.sh ;;
  esac
}

while true; do
  action="$(host_arrow_menu \
    "=== opencode-model-eval control ===" \
    "Deploy harness" "Remove harness" "Run an eval" "Quit")" || exit 0

  case "$action" in
    "Deploy harness") deploy || echo "Deploy failed -- see output above." >&2 ;;
    "Remove harness") remove || echo "Remove failed -- see output above." >&2 ;;
    "Run an eval") run_eval || echo "Eval run failed or was cancelled -- see output above." >&2 ;;
    Quit) exit 0 ;;
  esac
done
