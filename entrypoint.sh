#!/bin/bash
# entrypoint.sh — dispatcher with two modes, matching this repo's
# static-server + HTTP-client architecture (see docs/CODEGEN.md's
# Docker section for why there's no more per-model build/entrypoint):
#
#   entrypoint.sh serve        starts `opencode serve`, long-running.
#                               This is the CMD default (see Dockerfile).
#   entrypoint.sh eval-client  runs run_eval_client.py against a running
#                               serve instance over HTTP, once, and exits.
set -euo pipefail

readonly PORT="${OPENCODE_SERVE_PORT:-4096}"
readonly HOSTNAME_BIND="${OPENCODE_SERVE_HOSTNAME:-0.0.0.0}"
readonly AUTH_PATH="${HOME}/.local/share/opencode/auth.json"

log() {
  printf '[entrypoint] %s\n' "$1" >&2
}

fail() {
  log "FATAL: $1"
  exit 1
}

if ! command -v opencode >/dev/null 2>&1; then
  fail "opencode binary not found on PATH — base image contract has changed, re-verify against opencode.ai/docs"
fi

if [ ! -f "${AUTH_PATH}" ]; then
  fail "credentials not found at ${AUTH_PATH} — mount your host auth.json read-only to this path (see README)"
fi

mode="${1:-serve}"

case "${mode}" in
  serve)
    log "starting opencode serve on ${HOSTNAME_BIND}:${PORT}"
    log "HOME resolved to: ${HOME}"
    # --port/--hostname explicitly set: opencode's real defaults are
    # port=0 (random) and hostname=127.0.0.1 (loopback only) -- neither
    # works for a container another service needs to reach predictably.
    exec opencode serve --port "${PORT}" --hostname "${HOSTNAME_BIND}"
    ;;
  eval-client)
    shift || true
    server_url="${OPENCODE_SERVER_URL:-http://server:4096}"
    log "waiting for server at ${server_url} to accept connections..."
    attempt=0
    max_attempts=30
    until python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('${server_url}/session', timeout=3)
except Exception as e:
    # any HTTP response (even an error status) means the server is up
    # and answering -- only a connection-level failure means not ready
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        sys.exit(0)
    sys.exit(1)
sys.exit(0)
" 2>/dev/null; do
      attempt=$((attempt + 1))
      if [ "${attempt}" -ge "${max_attempts}" ]; then
        fail "server at ${server_url} did not become reachable after ${max_attempts} attempts (docker-compose's depends_on only waits for container start, not port readiness -- this loop covers that gap)"
      fi
      sleep 2
    done
    log "server reachable, running eval client against ${server_url}"
    exec python3 /usr/local/bin/run_eval_client.py "$@"
    ;;
  *)
    fail "unknown mode '${mode}' — expected 'serve' or 'eval-client'"
    ;;
esac
