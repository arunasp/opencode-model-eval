#!/bin/sh
# entrypoint.sh — dispatcher with three modes, matching this repo's
# static-server + HTTP-client architecture (see docs/CODEGEN.md's
# Docker section for why there's no more per-model build/entrypoint):
#
#   entrypoint.sh serve        starts `opencode serve`, long-running.
#                               This is the CMD default (see Dockerfile).
#   entrypoint.sh eval-client  runs run_eval_client.py against a running
#                               serve instance over HTTP, once, and exits.
#   entrypoint.sh jupyter      starts Jupyter Lab, long-running. The
#                               jupyter stage used to clear ENTRYPOINT
#                               and call `jupyter lab` from CMD, which
#                               meant it never reached the uid drop
#                               below -- and it is the one role that
#                               writes to a host bind mount (./notebooks)
#                               on every save.
#
# POSIX sh, not bash: the `server` stage's apk install list never
# included bash (only ca-certificates/python3/setpriv), so a
# #!/bin/bash shebang would fail the same confusing "not found" way
# fetch_embedding_model.sh's did -- the shell fails to find the
# INTERPRETER, not this file. `set -o pipefail` (the one bash-only
# bit) is dropped rather than worked around: there are no pipes
# anywhere in this script's actual logic, so it was never doing
# anything here either.
set -eu

log() {
  printf '[entrypoint] %s\n' "$1" >&2
}

fail() {
  log "FATAL: $1"
  exit 1
}

# --- privilege drop ----------------------------------------------------
# Every container built from this Dockerfile runs as root by default,
# so anything it writes to a bind-mounted host path (./results,
# ./notebooks) lands root-owned and unusable to the host user. Starting
# as root, creating a passwd entry for the runtime-supplied uid:gid, and
# re-exec'ing this script as that user fixes it at the source.
#
# The re-exec needs no marker variable: after the drop `id -u` is no
# longer 0, so the second pass falls straight through to the dispatcher.
#
# WORKER_UID/WORKER_GID first, matching cicd-runner's own worker
# contract, with TARGET_UID/TARGET_GID accepted as the alternative
# convention. Unset means run as root, unchanged -- the same
# degrades-gracefully shape every other optional setting here has.
#
# No --reset-env. It would clear OPENCODE_CONFIG, OPENCODE_MODEL_*,
# OPENCODE_SERVER_URL, OLLAMA_BASE_URL and, when this image is invoked
# as a cicd-runner worker, NPM_CONFIG_CACHE/CARGO_HOME/PIP_CACHE_DIR.
# That is not a fixed list that can be re-injected by name the way
# cicd-runner's own worker/entrypoint.sh does, so the environment is
# preserved instead of reset and rebuilt.
drop_uid="${WORKER_UID:-${TARGET_UID:-}}"
drop_gid="${WORKER_GID:-${TARGET_GID:-}}"

if [ -n "${drop_uid}" ] && [ -n "${drop_gid}" ] && [ "$(id -u)" = "0" ]; then
  # Resolve the mechanism BEFORE creating anything: a passwd entry
  # written and then abandoned is state changed for no reason. BusyBox
  # ships a setpriv applet carrying none of these flags, so test for
  # the flag rather than for the file -- `command -v setpriv` finds the
  # busybox symlink and tells you nothing.
  if setpriv --help 2>&1 | grep -q -- '--reuid'; then
    drop_cmd="setpriv --reuid=${drop_uid} --regid=${drop_gid} --clear-groups"
  elif command -v su-exec >/dev/null 2>&1; then
    drop_cmd="su-exec ${drop_uid}:${drop_gid}"
  else
    fail "WORKER_UID/WORKER_GID set but no usable privilege-drop binary (util-linux setpriv or su-exec). Refusing to run as root and leave root-owned files on the bind mounts."
  fi

  if ! getent passwd "${drop_uid}" >/dev/null 2>&1; then
    getent group "${drop_gid}" >/dev/null 2>&1 || addgroup -g "${drop_gid}" harness
    # BusyBox adduser chowns and chmods an existing home directory to
    # the new uid, unlike Debian's, so no explicit chown is needed on
    # this base. Confirmed by running it, not assumed.
    adduser -u "${drop_uid}" -G harness -h "${HOME}" -s /bin/sh -D harness >/dev/null 2>&1
  fi

  # The opencode log directory is a named volume, so it keeps whatever
  # ownership earlier runs left in it. Anything written there while this
  # image still ran as root is unopenable after the drop, and opencode
  # treats that as fatal: "PermissionDenied: FileSystem.open". Chowning
  # it here, as root, migrates an existing deployment on the next start
  # instead of requiring a manual step nobody would know to take.
  #
  # Scoped to this one directory on purpose. ${HOME} also holds the
  # read-only auth.json and opencode.json mounts, which cannot be
  # chowned, and the bind-mounted /results and /notebooks belong to the
  # host user already -- rewriting ownership there is the failure this
  # whole mechanism exists to avoid, not a fix for it.
  opencode_log_dir="${HOME}/.local/share/opencode/log"
  if [ -d "${opencode_log_dir}" ]; then
    chown -R "${drop_uid}:${drop_gid}" "${opencode_log_dir}" 2>/dev/null \
      || log "could not chown ${opencode_log_dir} -- opencode may fail to open its log"
  fi

  log "dropping to ${drop_uid}:${drop_gid}"
  # Word-splitting is intended: drop_cmd is assembled here from the two
  # numeric ids, never from a caller-supplied string.
  # shellcheck disable=SC2086
  exec ${drop_cmd} "$0" "$@"
fi
# --- end privilege drop ------------------------------------------------

readonly PORT="${OPENCODE_SERVE_PORT:-4096}"
readonly HOSTNAME_BIND="${OPENCODE_SERVE_HOSTNAME:-0.0.0.0}"
readonly AUTH_PATH="${HOME}/.local/share/opencode/auth.json"

mode="${1:-serve}"

# Validate the mode before anything else can fail on its behalf. With
# this check further down, a typo'd mode reported "credentials not
# found" -- the auth check ran first and never got as far as saying the
# mode was the problem.
case "${mode}" in
  serve|eval-client|jupyter) ;;
  *) fail "unknown mode '${mode}' -- expected 'serve', 'eval-client' or 'jupyter'" ;;
esac

if [ "${mode}" != "jupyter" ] && ! command -v opencode >/dev/null 2>&1; then
  fail "opencode binary not found on PATH — base image contract has changed, re-verify against opencode.ai/docs"
fi

# Credentials are NOT required for every mode. `serve` keeps the
# requirement -- the shared server routes BOTH local and cloud
# provider requests, so it needs to be ready for either. `eval-client`
# only needs it when targeting a real cloud provider: Ollama needs no
# authentication at all (config's "apiKey": "ollama" is a placeholder
# string, not a credential), so an eval-client run specifically
# targeting local/ollama has nothing to check credentials against.
# `jupyter` never talks to a provider itself -- a notebook reaches
# opencode over HTTP through the server container, which does its own
# credential check.
# Every other eval-client target (opencode, deepseek, zhipu, ...)
# still requires it, same as before.
needs_auth=true
if [ "${mode}" = "eval-client" ] && [ "${OPENCODE_MODEL_PROVIDER:-}" = "local/ollama" ]; then
  needs_auth=false
fi
if [ "${mode}" = "jupyter" ]; then
  needs_auth=false
fi

if [ "${needs_auth}" = "true" ] && [ ! -f "${AUTH_PATH}" ]; then
  fail "credentials not found at ${AUTH_PATH} — mount your host auth.json read-only to this path (see INSTALL.md's Setup section). Not required if you're running eval-client against local/ollama specifically."
fi

case "${mode}" in
  serve)
    # Auto-detect local Ollama models before opencode starts, so the
    # config reflects whatever's actually installed on the host right
    # now, additively on top of whatever your global opencode config
    # already declares (opencode's own config.ts:loadGlobal() merges
    # that in separately, upstream of this script -- see
    # discover_local_ollama_models.py's docstring). Graceful: if Ollama
    # is unreachable, this leaves local/ollama.models as-is rather than
    # blocking or failing startup -- your global config still covers
    # models in that case.
    runtime_config="${HOME}/.config/opencode/opencode.runtime.json"
    log "discovering local Ollama models before startup..."
    python3 /usr/local/bin/discover_local_ollama_models.py \
      --base-config "${OPENCODE_CONFIG}" \
      --ollama-tags-url "${OPENCODE_OLLAMA_TAGS_URL:-http://host.docker.internal:11434/api/tags}" \
      --output "${runtime_config}" \
      --provider-key "${OPENCODE_OLLAMA_PROVIDER_KEY:-local/ollama}" \
      --timeout "${OPENCODE_OLLAMA_DISCOVERY_TIMEOUT:-3}" \
      || log "discovery script itself failed unexpectedly (not just Ollama-unreachable) -- continuing with OPENCODE_CONFIG at ${OPENCODE_CONFIG} unchanged, your global config still covers models, not blocking startup over this"
    if [ -f "${runtime_config}" ]; then
      export OPENCODE_CONFIG="${runtime_config}"
    fi

    log "starting session reaper in the background (server-side TTL for abruptly-disconnected clients -- opencode has no native equivalent, see scripts/session_reaper.py)"
    python3 /usr/local/bin/session_reaper.py &

    # WARM THE CATALOG BEFORE SERVING, so the first real request does
    # not pay for it. `models --refresh` calls
    # ModelsDev.Service.refresh(true) (cli/cmd/models.ts:28-31), which
    # rewrites the models.dev cache under Global.Path.cache. Without
    # this, the fetch happens inside the first session's critical path
    # -- and in a restricted environment it fails slowly (models.dev and
    # models.opencode.ai both answer 403 from here, measured), which is
    # exactly the wrong moment to discover that.
    #
    # WHAT THIS DOES NOT DO, stated because it is the obvious
    # assumption: it does not pre-install provider SDK packages. The
    # install lives in getSDK (provider.ts:1836-1843) and is reached
    # only by a real request; `models` calls provider.list(), which
    # enumerates without instantiating. Providers in BUNDLED_PROVIDERS
    # (provider.ts:113-133, including @ai-sdk/openai-compatible, so all
    # of local/ollama) never install anything at all.
    #
    # NON-FATAL BY DESIGN. A cold or unreachable catalog must not stop
    # the server from starting: every model this harness runs is
    # declared in config, and the catalog only supplies metadata.
    # Timeout-bounded so an egress block cannot hang startup instead.
    if [ "${OPENCODE_WARM_CATALOG:-true}" = "true" ]; then
      log "warming the models.dev catalog cache before serving (opencode models --refresh)"
      if timeout "${OPENCODE_WARM_CATALOG_TIMEOUT_S:-60}" opencode models --refresh >/dev/null 2>&1; then
        log "catalog cache warmed"
      else
        log "catalog warm-up did not complete (rc=$?) -- continuing; models come from config, not the catalog"
      fi
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
  jupyter)
    shift || true
    jupyter_port="${JUPYTER_PORT_INTERNAL:-8888}"
    # --no-browser: there's no browser inside this container to open.
    # --ip=0.0.0.0: Jupyter's own default is loopback-only, unreachable
    # from outside the container, the same caveat opencode has.
    # No --allow-root: this runs as the dropped uid now, and keeping the
    # flag would hide a drop that silently failed to happen.
    # NotebookApp.token is left to Jupyter's own generated default
    # rather than disabled -- see docker-compose.yml/terraform for how
    # the token is surfaced to whoever started it.
    log "starting jupyter lab on 0.0.0.0:${jupyter_port} as uid $(id -u)"
    exec jupyter lab --ip=0.0.0.0 --port="${jupyter_port}" --no-browser "$@"
    ;;
esac
