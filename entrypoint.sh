#!/bin/sh
# entrypoint.sh — dispatcher with two modes, matching this repo's
# static-server + HTTP-client architecture (see docs/CODEGEN.md's
# Docker section for why there's no more per-model build/entrypoint):
#
#   entrypoint.sh serve        starts `opencode serve`, long-running.
#                               This is the CMD default (see Dockerfile).
#   entrypoint.sh eval-client  runs run_eval_client.py against a running
#                               serve instance over HTTP, once, and exits.
#
# POSIX sh, not bash: the `server` stage's apk install list never
# included bash (only ca-certificates/python3), so a #!/bin/bash
# shebang would fail the same confusing "not found" way
# fetch_embedding_model.sh's did -- the shell fails to find the
# INTERPRETER, not this file. `set -o pipefail` (the one bash-only
# bit) is dropped rather than worked around: there are no pipes
# anywhere in this script's actual logic, so it was never doing
# anything here either.
set -eu

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

mode="${1:-serve}"

# Credentials are NOT required for every mode. `serve` keeps the
# requirement -- the shared server routes BOTH local and cloud
# provider requests, so it needs to be ready for either. `eval-client`
# only needs it when targeting a real cloud provider: Ollama needs no
# authentication at all (config's "apiKey": "ollama" is a placeholder
# string, not a credential), so an eval-client run specifically
# targeting local/ollama has nothing to check credentials against.
# Every other eval-client target (opencode, deepseek, zhipu, ...)
# still requires it, same as before.
needs_auth=true
if [ "${mode}" = "eval-client" ] && [ "${OPENCODE_MODEL_PROVIDER:-}" = "local/ollama" ]; then
  needs_auth=false
fi

if [ "${needs_auth}" = "true" ] && [ ! -f "${AUTH_PATH}" ]; then
  fail "credentials not found at ${AUTH_PATH} — mount your host auth.json read-only to this path (see README). Not required if you're running eval-client against local/ollama specifically."
fi

case "${mode}" in
  serve)
    # Auto-detect local Ollama models before opencode starts, so the
    # config reflects whatever's actually installed on the host right
    # now rather than a hardcoded list going stale every time a model
    # is pulled/removed on Cyberdyne. Graceful: if Ollama is
    # unreachable, the discovery script writes the baked-in static
    # list unchanged (see discover_local_ollama_models.py) -- this is
    # never allowed to block or fail startup.
    runtime_config="${HOME}/.config/opencode/opencode.runtime.json"
    log "discovering local Ollama models before startup..."
    python3 /usr/local/bin/discover_local_ollama_models.py \
      --base-config "${OPENCODE_CONFIG}" \
      --ollama-tags-url "${OPENCODE_OLLAMA_TAGS_URL:-http://host.docker.internal:11434/api/tags}" \
      --output "${runtime_config}" \
      --provider-key "${OPENCODE_OLLAMA_PROVIDER_KEY:-local/ollama}" \
      --timeout "${OPENCODE_OLLAMA_DISCOVERY_TIMEOUT:-3}" \
      || log "discovery script itself failed unexpectedly (not just Ollama-unreachable) -- continuing with baked-in static config at ${OPENCODE_CONFIG}, not blocking startup over this"
    if [ -f "${runtime_config}" ]; then
      export OPENCODE_CONFIG="${runtime_config}"
    fi

    log "starting opencode serve on ${HOSTNAME_BIND}:${PORT}"
    log "HOME resolved to: ${HOME}"
    log "OPENCODE_CONFIG resolved to: ${OPENCODE_CONFIG}"
    # --port/--hostname explicitly set: opencode's real defaults are
    # port=0 (random) and hostname=127.0.0.1 (loopback only) -- neither
    # works for a container another service needs to reach predictably.
    # --print-logs: without this, opencode's structured logs only ever
    # go to a file (~/.local/share/opencode/log/opencode.log) -- the
    # actual error behind an HTTP 500 (e.g. ProviderModelNotFoundError)
    # was invisible in `docker logs` and needed `docker exec ... cat`
    # to find, live on Cyberdyne. Confirmed via opencode's own CLI docs
    # and source (anomalyco/opencode#13158's excerpt shows
    # `print: process.argv.includes("--print-logs")` reads this flag
    # correctly) that this mirrors the same log stream to stderr,
    # which `docker logs` captures directly. One known caveat, same
    # source: --log-level doesn't fully propagate to the file-writing
    # thread (stays INFO-capped there, open bug) -- --print-logs
    # itself isn't affected by that same bug, so the mirroring works
    # regardless, just capped at INFO detail rather than DEBUG.
    exec opencode serve --port "${PORT}" --hostname "${HOSTNAME_BIND}" --print-logs
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
