# shellcheck shell=bash
# scripts/lib/compose.sh -- resolve which Docker Compose CLI is present.
# Sourced by harness-control.sh, scripts/select-and-run-eval.sh and
# scripts/lib/server-lifecycle.sh; not meant to be run standalone (no
# shebang or executable bit, matching the other files in this
# directory). The Makefile reaches the same logic through
# scripts/compose.sh.
#
# Compose ships in two forms: the v2 `docker compose` subcommand and
# the v1 `docker-compose` standalone binary. Every call site in this
# repo used to name v1 directly, which is what broke `make server-up`
# on a machine carrying only v2. Resolution is lazy, so sourcing this
# file costs nothing on paths that never touch Compose.
#
# COMPOSE is an array because the v2 form is two words; calling it as
# "${COMPOSE[@]}" keeps that correct without word-splitting.

compose_resolve() {
  if [ "${COMPOSE_RESOLVED:-0}" = 1 ]; then
    return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    echo "error: no Docker Compose CLI on PATH -- install either the v2 plugin ('docker compose') or the v1 standalone binary ('docker-compose')" >&2
    return 127
  fi
  COMPOSE_RESOLVED=1
}

# Run Compose through the resolved CLI.
compose() {
  compose_resolve || return $?
  "${COMPOSE[@]}" "$@"
}

# Print the resolved CLI, for lines that echo a command before running
# it and for --dry-run output.
compose_str() {
  compose_resolve || return $?
  printf '%s' "${COMPOSE[*]}"
}
