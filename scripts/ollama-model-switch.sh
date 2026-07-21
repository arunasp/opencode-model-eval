#!/usr/bin/env bash
# ollama-model-switch.sh -- batch load/unload Ollama models over its
# HTTP API, not the `ollama` CLI (`ollama stop`/`ollama run`).
#
# Why the API and not the CLI: this repo's harness containers and any
# CI-driven eval run don't necessarily have the `ollama` binary on
# PATH (only the host running Ollama itself does) -- curl+jq against
# the API works from anywhere that can reach the Ollama port.
#
# Verified against Ollama's documented API (docs.ollama.com/api,
# docs.ollama.com/api/ps, docs.ollama.com/faq), not assumed from
# memory:
#   GET  /api/ps                                   -> currently loaded models
#   POST /api/generate {"model":X}                 -> loads/preloads X
#   POST /api/generate {"model":X,"keep_alive":0}   -> unloads X immediately
#
# Ollama only serializes model residency under memory pressure --
# loading a new model when there isn't room forces an implicit,
# timing-dependent unload of an idle one (default keep_alive is 5m).
# This script makes the switch explicit instead of relying on that.
#
# Both calls are genuinely slow on real hardware: unloading a large,
# partially CPU-offloaded model takes real time to flush (confirmed
# live: `ollama ps` shows a "Stopping..." transitional state), and
# Ollama queues a load request server-side until that memory is
# actually free (its own docs: "insufficient available memory...
# requests will be queued"). Rather than block on curl's own return
# and give no feedback, both the unload and the load run in the
# background and this script polls /api/ps itself, printing elapsed
# time so a slow-but-real wait is distinguishable from a hang, and
# failing loudly on a timeout instead of sitting forever.
#
# Usage:
#   bash scripts/ollama-model-switch.sh list
#   bash scripts/ollama-model-switch.sh switch-to qwen3-coder:30b
#   bash scripts/ollama-model-switch.sh unload-all
#
# Env:
#   OLLAMA_BASE_URL (default http://localhost:11434)
#   OLLAMA_SWITCH_TIMEOUT (default 600 -- seconds to wait for a single
#     unload or load before giving up; large CPU/GPU-split models can
#     legitimately take minutes)
#   OLLAMA_SWITCH_POLL_INTERVAL (default 3 -- seconds between /api/ps checks)
set -euo pipefail

: "${OLLAMA_BASE_URL:=http://localhost:11434}"
: "${OLLAMA_SWITCH_TIMEOUT:=600}"
: "${OLLAMA_SWITCH_POLL_INTERVAL:=3}"

for cmd in curl jq; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "error: ${cmd} is required (not found on PATH)" >&2
    exit 1
  fi
done

ollama_list_loaded() {
  # One loaded model name per line on stdout. Empty output means
  # nothing is loaded -- not an error.
  curl -sS "${OLLAMA_BASE_URL}/api/ps" | jq -r '.models[]?.name'
}

ollama_is_loaded() {
  ollama_list_loaded | grep -qFx "$1"
}

ollama_unload_bg() {
  # Fires the unload request in the background; caller polls for
  # completion with wait_until instead of blocking on this directly.
  local model="$1"
  curl -sS -X POST "${OLLAMA_BASE_URL}/api/generate" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "${model}" '{model: $m, keep_alive: 0}')" \
    >/dev/null 2>&1 &
  echo $!
}

ollama_load_bg() {
  # Fires the load request in the background; caller polls for
  # completion with wait_until instead of blocking on this directly.
  # Empty-prompt generate with no keep_alive override: preloads the
  # model, leaves it on Ollama's default 5m keep_alive.
  local model="$1"
  curl -sS -X POST "${OLLAMA_BASE_URL}/api/generate" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "${model}" '{model: $m}')" \
    >/dev/null 2>&1 &
  echo $!
}

# wait_until <description> <bg_pid> <target_state: loaded|unloaded> <model>
# Polls /api/ps every OLLAMA_SWITCH_POLL_INTERVAL seconds, printing
# elapsed time, until the model reaches the target state or
# OLLAMA_SWITCH_TIMEOUT is hit. Returns non-zero (without killing the
# still-running background request) on timeout, so a real slow
# operation isn't silently abandoned -- the caller decides what to do
# with a timed-out switch.
wait_until() {
  local desc="$1" bg_pid="$2" target_state="$3" model="$4"
  local elapsed=0

  while true; do
    if [ "${target_state}" = "loaded" ] && ollama_is_loaded "${model}"; then
      break
    fi
    if [ "${target_state}" = "unloaded" ] && ! ollama_is_loaded "${model}"; then
      break
    fi
    if [ "${elapsed}" -ge "${OLLAMA_SWITCH_TIMEOUT}" ]; then
      echo "TIMEOUT after ${elapsed}s waiting for: ${desc} (still running in background, pid ${bg_pid})" >&2
      return 1
    fi
    echo "  ...still waiting for ${desc} (${elapsed}s elapsed)" >&2
    sleep "${OLLAMA_SWITCH_POLL_INTERVAL}"
    elapsed=$((elapsed + OLLAMA_SWITCH_POLL_INTERVAL))
  done

  # Reap the background curl now that /api/ps confirms the state
  # change; best-effort only, since the process may already be gone.
  wait "${bg_pid}" 2>/dev/null || true
  echo "${desc}: confirmed after ${elapsed}s" >&2
}

cmd_unload_all() {
  local loaded
  loaded="$(ollama_list_loaded)"
  if [ -z "${loaded}" ]; then
    echo "Nothing loaded." >&2
    return 0
  fi
  local model pid
  while IFS= read -r model; do
    [ -z "${model}" ] && continue
    echo "Unloading ${model}..." >&2
    pid="$(ollama_unload_bg "${model}")"
    wait_until "unload of ${model}" "${pid}" unloaded "${model}"
  done <<< "${loaded}"
}

cmd_switch_to() {
  local target="$1"
  local loaded already_loaded model pid
  loaded="$(ollama_list_loaded)"
  already_loaded=false

  if [ -n "${loaded}" ]; then
    while IFS= read -r model; do
      [ -z "${model}" ] && continue
      if [ "${model}" = "${target}" ]; then
        already_loaded=true
        continue
      fi
      echo "Unloading ${model} (switching to ${target})..." >&2
      pid="$(ollama_unload_bg "${model}")"
      wait_until "unload of ${model}" "${pid}" unloaded "${model}"
    done <<< "${loaded}"
  fi

  if [ "${already_loaded}" = true ]; then
    echo "${target} already loaded." >&2
  else
    echo "Loading ${target}..." >&2
    pid="$(ollama_load_bg "${target}")"
    wait_until "load of ${target}" "${pid}" loaded "${target}"
  fi
}

cmd_list() {
  local loaded
  loaded="$(ollama_list_loaded)"
  if [ -z "${loaded}" ]; then
    echo "Nothing loaded."
  else
    echo "${loaded}"
  fi
}

case "${1:-}" in
  list)
    cmd_list
    ;;
  switch-to)
    if [ -z "${2:-}" ]; then
      echo "usage: $0 switch-to <model>" >&2
      exit 1
    fi
    cmd_switch_to "$2"
    ;;
  unload-all)
    cmd_unload_all
    ;;
  *)
    echo "usage: $0 {list|switch-to <model>|unload-all}" >&2
    exit 1
    ;;
esac
