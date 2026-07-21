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
# This script makes the switch explicit and immediate instead of
# relying on that: unload everything that isn't the target, then load
# the target, so two local eval runs in a row never silently share (or
# fight over) VRAM.
#
# Usage:
#   bash scripts/ollama-model-switch.sh list
#   bash scripts/ollama-model-switch.sh switch-to qwen3-coder:30b
#   bash scripts/ollama-model-switch.sh unload-all
#
# Env:
#   OLLAMA_BASE_URL (default http://localhost:11434)
set -euo pipefail

: "${OLLAMA_BASE_URL:=http://localhost:11434}"

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

ollama_unload() {
  local model="$1"
  curl -sS -X POST "${OLLAMA_BASE_URL}/api/generate" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "${model}" '{model: $m, keep_alive: 0}')" \
    >/dev/null
}

ollama_load() {
  local model="$1"
  # Empty-prompt generate with no keep_alive override: preloads the
  # model, leaves it on Ollama's default 5m keep_alive.
  curl -sS -X POST "${OLLAMA_BASE_URL}/api/generate" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "${model}" '{model: $m}')" \
    >/dev/null
}

cmd_unload_all() {
  local loaded
  loaded="$(ollama_list_loaded)"
  if [ -z "${loaded}" ]; then
    echo "Nothing loaded." >&2
    return 0
  fi
  while IFS= read -r model; do
    [ -z "${model}" ] && continue
    echo "Unloading ${model}..." >&2
    ollama_unload "${model}"
  done <<< "${loaded}"
}

cmd_switch_to() {
  local target="$1"
  local loaded already_loaded
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
      ollama_unload "${model}"
    done <<< "${loaded}"
  fi

  if [ "${already_loaded}" = true ]; then
    echo "${target} already loaded." >&2
  else
    echo "Loading ${target}..." >&2
    ollama_load "${target}"
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
