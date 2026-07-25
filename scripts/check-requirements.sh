#!/bin/bash
# Checks the host machine against REQUIREMENTS.md. Read-only -- never
# installs anything itself, just reports what's present/missing/wrong
# version so you know what to fix before running into a confusing
# failure three steps into Setup.
#
# Usage:
#   bash scripts/check-requirements.sh              # always-required + whichever paths are relevant
#   bash scripts/check-requirements.sh --terraform   # also check the Terraform path's requirements
#   bash scripts/check-requirements.sh --ollama      # also check local-Ollama requirements
#   bash scripts/check-requirements.sh --all         # check everything, including optional/dev-only
set -euo pipefail

WANT_TERRAFORM=false
WANT_OLLAMA=false
WANT_OPTIONAL=false
for arg in "$@"; do
  case "${arg}" in
    --terraform) WANT_TERRAFORM=true ;;
    --ollama) WANT_OLLAMA=true ;;
    --all) WANT_TERRAFORM=true; WANT_OLLAMA=true; WANT_OPTIONAL=true ;;
    *) echo "unknown argument: ${arg} (expected --terraform, --ollama, or --all)" >&2; exit 1 ;;
  esac
done

FAILED=0
ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
section() { printf '\n%s\n' "$1"; }

check_bin() {
  # check_bin <name> <label> <hard|soft>
  local name="$1" label="$2" severity="$3"
  if command -v "${name}" >/dev/null 2>&1; then
    ok "${label} found ($(command -v "${name}"))"
  elif [ "${severity}" = "hard" ]; then
    bad "${label} NOT found on PATH -- required, see REQUIREMENTS.md"
  else
    warn "${label} not found on PATH -- optional, see REQUIREMENTS.md"
  fi
}

section "Always required"
check_bin docker "Docker" hard
if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  bad "Docker daemon not reachable (is it running? do you have permission?)"
fi
if command -v "docker-compose" >/dev/null 2>&1; then
  ok "docker-compose (legacy standalone CLI) found: $(docker-compose --version 2>&1)"
elif docker compose version >/dev/null 2>&1; then
  ok "docker compose (v2 plugin) found: $(docker compose version 2>&1)"
else
  bad "neither 'docker-compose' nor 'docker compose' found -- one is required"
fi
check_bin bash "bash" hard
check_bin jq "jq" hard
if command -v python3 >/dev/null 2>&1; then
  PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info[0])')"
  PY_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
  if [ "${PY_MAJOR}" -gt 3 ] || { [ "${PY_MAJOR}" -eq 3 ] && [ "${PY_MINOR}" -ge 10 ]; }; then
    ok "python3 ${PY_VERSION} found (>= 3.10 required for this repo's own \`X | None\` type syntax)"
  else
    bad "python3 ${PY_VERSION} found, but < 3.10 -- discover_and_select_model.py and others use \`X | None\` union syntax that needs 3.10+"
  fi
else
  bad "python3 not found on PATH -- required"
fi

section "Primary entry point (harness-control.sh)"
check_bin tmux "tmux" hard
if [ -t 0 ]; then
  ok "running in a real terminal (harness-control.sh's model picker needs one)"
else
  warn "not running in a real terminal -- harness-control.sh needs one; scripted/CI use falls back to auto-pick instead, which is fine for that path"
fi

if [ "${WANT_TERRAFORM}" = "true" ]; then
  section "Terraform path"
  check_bin terraform "terraform" hard
  if command -v terraform >/dev/null 2>&1; then
    TF_VERSION="$(terraform version -json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || echo "unknown")"
    ok "terraform version: ${TF_VERSION} (need >= 1.1.5, checked by terraform itself via required_version)"
  fi
fi

if [ "${WANT_OLLAMA}" = "true" ]; then
  section "Local Ollama path"
  check_bin curl "curl" hard
  OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
  if curl -sS -m 3 "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    ok "Ollama reachable at ${OLLAMA_URL}"
  else
    bad "Ollama NOT reachable at ${OLLAMA_URL} -- is it running? Was it started with OLLAMA_HOST=0.0.0.0:11434? (its default, loopback-only, is not reachable from inside a container)"
  fi
fi

if [ "${WANT_OPTIONAL}" = "true" ]; then
  section "Optional (development/testing only)"
  check_bin node "node" soft
  check_bin npm "npm" soft
  check_bin shellcheck "shellcheck" soft
fi

section "Summary"
if [ "${FAILED}" -eq 0 ]; then
  echo "All checked requirements satisfied."
else
  echo "One or more required dependencies are missing or misconfigured -- see [FAIL] lines above and REQUIREMENTS.md." >&2
  exit 1
fi
