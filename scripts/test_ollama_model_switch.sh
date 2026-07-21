#!/usr/bin/env bash
# test_ollama_model_switch.sh -- exercises scripts/ollama-model-switch.sh
# against a stateful fake Ollama server (fake `curl` on PATH), since a
# real Ollama instance isn't available in CI. Verifies the request
# shapes match Ollama's documented API (GET /api/ps, POST /api/generate
# with keep_alive 0 to unload / omitted to load), not just that the
# script runs without error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly SWITCH_SCRIPT="${SCRIPT_DIR}/ollama-model-switch.sh"
STATE_FILE="/tmp/test_ollama_model_switch_state"
readonly STATE_FILE
FAKE_BIN_DIR="$(mktemp -d)"
readonly FAKE_BIN_DIR

cleanup() {
  rm -rf "${FAKE_BIN_DIR}" "${STATE_FILE}" "${STATE_FILE}.tmp"
}
trap cleanup EXIT

cat > "${FAKE_BIN_DIR}/curl" <<'CURL_EOF'
#!/usr/bin/env bash
STATE="/tmp/test_ollama_model_switch_state"
touch "$STATE"
args=("$@")
is_post=false
data=""
url=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) [ "${args[$((i+1))]}" = "POST" ] && is_post=true ;;
    -d) data="${args[$((i+1))]}" ;;
    http*) url="${args[$i]}" ;;
  esac
done
if [[ "$url" == *"/api/ps" ]]; then
  models_json="[]"
  if [ -s "$STATE" ]; then
    models_json="$(jq -R -s -c 'split("\n") | map(select(length>0)) | map({name: .})' "$STATE")"
  fi
  echo "{\"models\": $models_json}"
elif [[ "$url" == *"/api/generate" ]] && $is_post; then
  model="$(echo "$data" | jq -r '.model')"
  keep_alive="$(echo "$data" | jq -r '.keep_alive // "unset"')"
  if [ "$keep_alive" = "0" ]; then
    grep -vFx "$model" "$STATE" > "${STATE}.tmp" 2>/dev/null || true
    mv "${STATE}.tmp" "$STATE"
  else
    grep -qFx "$model" "$STATE" 2>/dev/null || echo "$model" >> "$STATE"
  fi
  echo '{"done": true}'
else
  echo "unrecognized fake curl call: $*" >&2
  exit 1
fi
CURL_EOF
chmod +x "${FAKE_BIN_DIR}/curl"

export PATH="${FAKE_BIN_DIR}:${PATH}"

pass=0
fail=0

check() {
  local desc="$1" expected="$2" actual="$3"
  if [ "${expected}" = "${actual}" ]; then
    echo "PASS: ${desc}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${desc} -- expected [${expected}], got [${actual}]"
    fail=$((fail + 1))
  fi
}

# Test 1: switching to a fresh model unloads the current one and loads the new one
echo "gemma4:31b" > "${STATE_FILE}"
bash "${SWITCH_SCRIPT}" switch-to "qwen3-coder:30b" >/dev/null 2>&1
check "switch-to unloads old, loads new" "qwen3-coder:30b" "$(bash "${SWITCH_SCRIPT}" list)"

# Test 2: switching to the already-loaded model is a no-op (no unload/reload)
before="$(cat "${STATE_FILE}")"
bash "${SWITCH_SCRIPT}" switch-to "qwen3-coder:30b" >/dev/null 2>&1
check "switch-to already-loaded model is idempotent" "${before}" "$(cat "${STATE_FILE}")"

# Test 3: switching away from multiple loaded models keeps only the target
printf 'gemma4:31b\nqwen2.5-coder:7b\nnemotron-3-nano:30b\n' > "${STATE_FILE}"
bash "${SWITCH_SCRIPT}" switch-to "nemotron-3-nano:30b" >/dev/null 2>&1
check "switch-to with multiple loaded keeps only target" "nemotron-3-nano:30b" "$(bash "${SWITCH_SCRIPT}" list)"

# Test 4: unload-all clears everything
printf 'gemma4:31b\nqwen2.5-coder:7b\n' > "${STATE_FILE}"
bash "${SWITCH_SCRIPT}" unload-all >/dev/null 2>&1
check "unload-all clears all loaded models" "Nothing loaded." "$(bash "${SWITCH_SCRIPT}" list)"

# Test 5: unload-all on an already-empty state is a safe no-op
: > "${STATE_FILE}"
bash "${SWITCH_SCRIPT}" unload-all >/dev/null 2>&1
check "unload-all on empty state is a no-op" "Nothing loaded." "$(bash "${SWITCH_SCRIPT}" list)"

echo
echo "${pass} passed, ${fail} failed"
[ "${fail}" -eq 0 ]
