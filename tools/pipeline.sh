#!/usr/bin/env bash
# tools/pipeline.sh -- staged checks for this repo.
#
# Stages: lint, test, verify. `all` runs them in that order and reports
# every failure rather than stopping at the first one.
#
# A stage that exits 2 counts as SKIPPED, not failed: it means a tool the
# stage needs is absent from this environment. That distinction matters
# because the same pipeline runs in a cicd_runner worker, in a sandbox and
# on a developer machine, and those carry different toolchains.
#
# Every run is tee'd to logs/<UTC>-stages.log.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-stages.log"

FAILED_STAGES=""
SKIPPED_STAGES=""

log() { printf '%s\n' "$*"; }

# Stages are dispatched by name rather than by function reference, so the
# call sites stay statically visible to ShellCheck.
run_stage() {
  local name="$1"
  local rc
  log ""
  log "=== stage: $name ==="
  case "$name" in
    lint) stage_lint ;;
    test) stage_test ;;
    verify) stage_verify ;;
    *) log "unknown stage: $name"; return 1 ;;
  esac
  rc=$?
  case "$rc" in
    0) log "--- $name: PASS" ;;
    2) log "--- $name: SKIPPED"; SKIPPED_STAGES="$SKIPPED_STAGES $name" ;;
    *) log "--- $name: FAIL (exit $rc)"; FAILED_STAGES="$FAILED_STAGES $name" ;;
  esac
  return 0
}

shell_files() {
  printf '%s\n' \
    harness-control.sh \
    entrypoint.sh \
    scripts/compose.sh \
    scripts/lib/compose.sh \
    scripts/lib/server-lifecycle.sh \
    scripts/lib/host-model-picker.sh \
    scripts/lib/opencode-global-config.sh \
    scripts/select-and-run-eval.sh \
    scripts/tf-select-and-run-eval.sh \
    scripts/ensure-auth-data.sh \
    scripts/extract-opencode-key.sh \
    scripts/tf-extract-auth-keys.sh \
    scripts/ollama-model-switch.sh \
    scripts/check-requirements.sh \
    scripts/fetch_embedding_model.sh \
    scripts/test_ollama_model_switch.sh \
    scripts/test_compose_detection.sh
}

stage_lint() {
  local rc=0

  if command -v shellcheck >/dev/null 2>&1; then
    local f
    while read -r f; do
      [ -f "$f" ] || continue
      shellcheck -x "$f" || rc=1
    done < <(shell_files)
    log "shellcheck: done"
  else
    log "shellcheck not installed"
    return 2
  fi

  local p
  for p in scripts/*.py scripts/tools/*.py; do
    [ -f "$p" ] || continue
    python3 -m py_compile "$p" || rc=1
  done
  log "py_compile: done"

  if command -v pycodestyle >/dev/null 2>&1; then
    pycodestyle --max-line-length=120 scripts/*.py || rc=1
    log "pycodestyle: done"
  else
    log "pycodestyle not installed, skipping that check"
  fi

  local j
  for j in config/*.json; do
    [ -f "$j" ] || continue
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$j" || rc=1
  done
  log "json parse: done"

  return "$rc"
}

stage_test() {
  local rc=0 t

  for t in scripts/test_*.py; do
    [ -f "$t" ] || continue
    log "python3 $t"
    python3 "$t" || rc=1
  done

  for t in scripts/test_*.sh; do
    [ -f "$t" ] || continue
    # test_ollama_model_switch.sh drives the switch script, which hard-requires
    # jq. Without it the test fails for an environment reason rather than a
    # code reason, which is exactly the distinction a red stage should not blur.
    if [ "$t" = "scripts/test_ollama_model_switch.sh" ] && ! command -v jq >/dev/null 2>&1; then
      log "skipping $t: jq not installed"
      continue
    fi
    log "bash $t"
    bash "$t" || rc=1
  done

  return "$rc"
}

stage_verify() {
  local rc=0

  # Every Compose call site must go through the resolver, or the repo
  # breaks again on a machine carrying only one of the two CLIs.
  local stray
  # tools/pipeline.sh is excluded because it names the string in order to
  # search for it -- without this the check matches its own source.
  stray="$(grep -rn 'docker-compose ' --include='*.sh' --include=Makefile . 2>/dev/null \
    | grep -vE ':[0-9]+:[[:space:]]*#' \
    | grep -vE 'scripts/(lib/)?compose\.sh|scripts/check-requirements\.sh|tools/pipeline\.sh' || true)"
  if [ -n "$stray" ]; then
    log "call sites naming the v1 binary directly:"
    log "$stray"
    rc=1
  else
    log "no stray docker-compose call sites"
  fi

  # The Makefile must reach Compose through the wrapper.
  # Captured first rather than piped into grep -q: with pipefail set, grep
  # exiting early sends SIGPIPE to make, and the pipeline status becomes 141
  # even on a match.
  local recipe
  recipe="$(make -n server-up 2>/dev/null || true)"
  if printf '%s' "$recipe" | grep -q 'scripts/compose.sh'; then
    log "make server-up resolves through scripts/compose.sh"
  else
    log "make server-up does not use scripts/compose.sh"
    rc=1
  fi

  # The wrapper is useless without its executable bit committed.
  if [ "$(git ls-files -s scripts/compose.sh | cut -c1-6)" = "100755" ]; then
    log "scripts/compose.sh committed executable"
  else
    log "scripts/compose.sh is not committed with mode 100755"
    rc=1
  fi

  return "$rc"
}

usage() {
  cat <<USAGE
usage: tools/pipeline.sh [lint|test|verify|all]
USAGE
}

main() {
  local target="${1:-all}"
  case "$target" in
    lint) run_stage lint ;;
    test) run_stage test ;;
    verify) run_stage verify ;;
    all)
      run_stage lint
      run_stage test
      run_stage verify
      ;;
    -h|--help) usage; return 0 ;;
    *) usage; return 1 ;;
  esac

  log ""
  [ -n "$SKIPPED_STAGES" ] && log "SKIPPED:$SKIPPED_STAGES"
  if [ -n "$FAILED_STAGES" ]; then
    log "RESULT: FAILED$FAILED_STAGES"
    return 1
  fi
  log "RESULT: ALL PASS"
  return 0
}

main "$@" 2>&1 | tee "$LOG_FILE"
exit "${PIPESTATUS[0]}"
