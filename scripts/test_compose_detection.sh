#!/usr/bin/env bash
# Verifies that scripts/lib/compose.sh picks the right Compose CLI, by
# faking the binaries on PATH rather than needing both installed.
#
# Three scenarios, and the third is the one that matters: with neither CLI
# present the resolver must fail loudly rather than defaulting to something
# that appears to work.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

mock_dir() {
  local dir
  dir="$(mktemp -d)"
  printf '%s' "$dir"
}

# Scenario 1: v2 plugin present, no standalone binary.
V2="$(mock_dir)"
cat > "$V2/docker" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "compose" ]; then
  shift
  [ "${1:-}" = "version" ] && { echo "Docker Compose version v2.29.0"; exit 0; }
  echo "MOCK-V2 ran: compose $*"
  exit 0
fi
echo "MOCK-V2 docker $*"
SH
chmod +x "$V2/docker"

# Scenario 2: no plugin, legacy standalone present.
V1="$(mock_dir)"
cat > "$V1/docker" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "compose" ]; then
  echo "docker: 'compose' is not a docker command." >&2
  exit 1
fi
echo "MOCK-V1 docker $*"
SH
cat > "$V1/docker-compose" <<'SH'
#!/usr/bin/env bash
echo "MOCK-V1 ran: $*"
SH
chmod +x "$V1/docker" "$V1/docker-compose"

# Scenario 3: neither.
NONE="$(mock_dir)"
cat > "$NONE/docker" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "compose" ]; then
  echo "docker: 'compose' is not a docker command." >&2
  exit 1
fi
echo "MOCK-NONE docker $*"
SH
chmod +x "$NONE/docker"

run_wrapper() {
  local dir="$1"
  shift
  PATH="$dir:/usr/bin:/bin" bash "$REPO_ROOT/scripts/compose.sh" "$@" 2>&1
}

out="$(run_wrapper "$V2" up -d server)"
check "v2 plugin is used" "MOCK-V2 ran: compose up -d server" "$out"

out="$(run_wrapper "$V1" up -d server)"
check "v1 binary is the fallback" "MOCK-V1 ran: up -d server" "$out"

out="$(run_wrapper "$V1" up -d server)"
case "$out" in
  *MOCK-V2*) check "v1 path does not invoke the v2 form" "no v2" "v2 invoked" ;;
  *) check "v1 path does not invoke the v2 form" "no v2" "no v2" ;;
esac

out="$(run_wrapper "$NONE" up -d server)"
rc=$?
case "$out" in
  *"no Docker Compose CLI on PATH"*) check "neither present fails loudly" "named error" "named error" ;;
  *) check "neither present fails loudly" "named error" "$out" ;;
esac
check "neither present exits non-zero" "nonzero" "$([ "$rc" -ne 0 ] && echo nonzero || echo zero)"

# The sourced-library path, which the scripts use directly.
probe="$(mktemp)"
cat > "$probe" <<SH
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_ROOT"
source scripts/lib/compose.sh
compose_str
echo
compose ps
SH
out="$(PATH="$V2:/usr/bin:/bin" bash "$probe" 2>&1)"
case "$out" in
  "docker compose"*"MOCK-V2 ran: compose ps"*) check "sourced library resolves and runs" "ok" "ok" ;;
  *) check "sourced library resolves and runs" "ok" "$out" ;;
esac

rm -rf "$V2" "$V1" "$NONE" "$probe"

echo
echo "=== compose detection: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
