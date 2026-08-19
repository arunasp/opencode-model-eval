#!/usr/bin/env bash
# tools/pipeline.sh -- staged checks for this repo.
#
# Stages: lint, test, verify, e2e, client, containers. `all` runs
# lint/test/verify/e2e/client in that order and reports every failure
# rather than stopping at the first one.
#
# `containers` is deliberately NOT in `all`. It builds images, starts a
# real server and stops it again -- a stateful action on a shared
# machine that has to be asked for rather than arriving as a side
# effect of a check run.
#
# A stage that exits 2 counts as SKIPPED, not failed: it means a tool the
# stage needs is absent from this environment. That distinction matters
# because the same pipeline runs in a cicd_runner worker, in a sandbox and
# on a developer machine, and those carry different toolchains. The two
# new stages lean on it heavily: a worker has no docker socket at all, so
# `containers` can only ever skip there, and `client` skips unless some
# server is actually answering.
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

# The Claude Desktop Filesystem connector writes files without an
# executable bit, so any script it edits comes back mode 644 and git
# records a mode change on the next commit -- which then blocks a later
# checkout of that path. This has cost several amend cycles.
#
# The list comes from git itself rather than being maintained here: only
# a path already committed 100755 is touched, so this can never add an
# executable bit that was not already recorded. The reverse case (a file
# executable in the worktree but committed 644) is deliberately left
# alone -- that is a real decision to make, not drift to paper over, and
# stage_verify flags it.
#
# Called from main() on every invocation, not offered as a step to
# remember: a fix that has to be invoked is a fix that gets skipped.
restore_exec_bits() {
  command -v git >/dev/null 2>&1 || return 0
  git rev-parse --git-dir >/dev/null 2>&1 || return 0

  local path restored=0 rc=0
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    [ -f "$path" ] || continue
    [ -x "$path" ] && continue
    if chmod +x "$path"; then
      log "restored executable bit: $path"
      restored=$((restored + 1))
    else
      log "could not restore executable bit: $path"
      rc=1
    fi
  done < <(git ls-files -s | grep '^100755 ' | cut -f2)

  [ "$restored" -gt 0 ] && log "exec bits restored: $restored file(s)"
  return "$rc"
}

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
    e2e) stage_e2e ;;
    client) stage_client ;;
    containers) stage_containers ;;
    exec-bits) restore_exec_bits ;;
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

  # Absorbed from the former ad-hoc verify.sh scratch script (checks
  # 1-4 there -- deterministic, no server needed, unlike check 5 which
  # is now stage_e2e below). Confirms scripts/lib/opencode-global-config.sh
  # is genuinely wired into all three of its real call sites, and that
  # its own default-resolution behavior actually works both ways --
  # not just that the file exists.
  local sourced_by
  sourced_by="$(grep -l "source scripts/lib/opencode-global-config.sh" \
    harness-control.sh scripts/select-and-run-eval.sh scripts/tf-select-and-run-eval.sh 2>/dev/null | wc -l)"
  if [ "$sourced_by" = "3" ]; then
    log "opencode-global-config.sh sourced by all 3 real call sites"
  else
    log "opencode-global-config.sh sourced by only $sourced_by/3 expected call sites"
    rc=1
  fi

  local resolved_default expected_default
  resolved_default="$(env -u OPENCODE_GLOBAL_CONFIG bash -c     'source scripts/lib/opencode-global-config.sh; echo "$OPENCODE_GLOBAL_CONFIG"')"
  expected_default="$HOME/.config/opencode/opencode.json"
  if [ "$resolved_default" = "$expected_default" ]; then
    log "default resolves correctly when unset ($expected_default)"
  else
    log "default resolution wrong: got '$resolved_default', expected '$expected_default'"
    rc=1
  fi

  local preserved
  preserved="$(OPENCODE_GLOBAL_CONFIG=/tmp/x bash -c     'source scripts/lib/opencode-global-config.sh; echo "$OPENCODE_GLOBAL_CONFIG"')"
  if [ "$preserved" = "/tmp/x" ]; then
    log "an already-set value is left untouched"
  else
    log "an already-set value was overwritten: got '$preserved', expected '/tmp/x'"
    rc=1
  fi

  local make_recipe
  make_recipe="$(env -u OPENCODE_GLOBAL_CONFIG make --no-print-directory --eval='p: ; @echo \$(OPENCODE_GLOBAL_CONFIG)' p 2>/dev/null)"
  if [ "$make_recipe" = "$expected_default" ]; then
    log "Makefile resolves the same default ($expected_default)"
  else
    log "Makefile default mismatch: got '$make_recipe', expected '$expected_default'"
    rc=1
  fi

  # This repository is public, so a real host path or local username in a
  # tracked file is a permanent disclosure -- worse than the drift a
  # mismatched path causes, because rewriting published history is the
  # only way back. Container-internal paths under /home/harness and
  # /home/worker are legitimate and allowed; anything else naming a home
  # directory is a leak.
  #
  # This is also why HARNESS_ROOT lives in .env rather than in the
  # compose file: the one place the absolute path is genuinely needed is
  # the one file that is never committed.
  local path_leaks
  path_leaks="$(git grep -nE '/(home|Users)/[a-zA-Z0-9_.-]+' -- . 2>/dev/null \
    | grep -vE '/(home)/(harness|worker|YOUR_USER)([/"'"'"':[:space:]]|$)' || true)"
  if [ -n "$path_leaks" ]; then
    log "host paths in tracked files (this repo is public):"
    log "$path_leaks"
    rc=1
  else
    log "no host paths or local usernames in tracked files"
  fi

  # A note, not a failure: the development host's name is already in the
  # published history in a number of files, so failing on it would just
  # keep the stage red without removing anything. Reported so new ones
  # are visible as they appear.
  local host_names
  host_names="$(git grep -clE 'Cyberdyne' -- . 2>/dev/null | wc -l)"
  if [ "$host_names" -gt 0 ]; then
    log "note: development host name appears in $host_names tracked file(s) (pre-existing, already published)"
  fi

  # Bind sources must not be bare-relative. Compose resolves them against
  # the project directory as seen by whichever process builds the
  # command, and an orchestrator's view is not the daemon's -- confirmed
  # live, where `./auth-data/auth.json` became a /dynamic-root path and
  # the daemon created an empty directory tree at the host root instead
  # of failing. Every source goes through ${HARNESS_ROOT:-.}.
  local bare_relative
  bare_relative="$(grep -nE '^[[:space:]]*- \./' docker-compose.yml || true)"
  if [ -n "$bare_relative" ]; then
    log "bind sources bypassing HARNESS_ROOT:"
    log "$bare_relative"
    rc=1
  else
    log "every compose bind source goes through HARNESS_ROOT"
  fi

  # When it is set, it has to name this directory. A stale value from a
  # moved or copied checkout points the mounts somewhere real and wrong,
  # which is worse than pointing them somewhere missing.
  if [ -n "${HARNESS_ROOT:-}" ]; then
    local declared actual
    declared="$(cd "$HARNESS_ROOT" 2>/dev/null && pwd || echo '<unreadable>')"
    actual="$(pwd)"
    if [ "$declared" = "$actual" ]; then
      log "HARNESS_ROOT resolves to this directory"
    else
      log "HARNESS_ROOT is '$HARNESS_ROOT' -> '$declared', but this directory is '$actual'"
      rc=1
    fi
  else
    log "HARNESS_ROOT unset (correct for shell use; required for an orchestrator)"
  fi

  # A mode-only diff blocks a later checkout of that path, so it should
  # never survive into a commit. restore_exec_bits() handles the common
  # direction automatically; what reaches here is the other one -- a file
  # made executable in the worktree but committed 644, which is a real
  # decision to make rather than connector damage to paper over.
  local mode_changes
  mode_changes="$(git diff --summary 2>/dev/null | grep 'mode change' || true)"
  if [ -n "$mode_changes" ]; then
    log "uncommitted mode changes:"
    log "$mode_changes"
    rc=1
  else
    log "no uncommitted file-mode changes"
  fi

  return "$rc"
}

stage_e2e() {
  local rc=0

  # Absorbed from the former ad-hoc verify.sh (check 5 there) -- the
  # one genuinely e2e check, needing a real Ollama to mean anything.
  # discover_local_ollama_models.py is already documented to degrade
  # gracefully (writes the base config unchanged, never hard-fails, if
  # Ollama is unreachable -- confirmed via its own --help text, not
  # assumed) -- so THIS stage's own job is to distinguish "nothing to
  # discover against" (SKIP, exit 2, matching the jq-missing pattern
  # elsewhere in this file) from "a real Ollama answered and the
  # discovered model list actually came back populated" (a real PASS,
  # not just "the script did not crash").
  local tags_url="${OPENCODE_OLLAMA_TAGS_URL:-http://localhost:11434/api/tags}"
  if ! python3 -c "
 import sys, urllib.request
 try:
     urllib.request.urlopen('$tags_url', timeout=2)
 except Exception:
     sys.exit(1)
 " 2>/dev/null; then
    log "Ollama not reachable at $tags_url -- skipping (set OPENCODE_OLLAMA_TAGS_URL to point elsewhere)"
    return 2
  fi

  local scratch base_config output
  scratch="$(mktemp -d)"
  base_config="$scratch/base.json"
  output="$scratch/runtime.json"
  echo '{"provider":{"local/ollama":{"models":{}}}}' > "$base_config"

  if ! python3 scripts/discover_local_ollama_models.py       --base-config "$base_config" --ollama-tags-url "$tags_url"       --output "$output" --provider-key local/ollama --timeout 3; then
    log "discover_local_ollama_models.py exited non-zero against a reachable Ollama"
    rc=1
  else
    local model_count
    model_count="$(python3 -c "
 import json
 data = json.load(open('$output'))
 print(len(data.get('provider', {}).get('local/ollama', {}).get('models', {})))
 ")"
    if [ "$model_count" -gt 0 ]; then
      log "local model list populated: $model_count model(s) discovered"
    else
      log "Ollama answered but zero models were discovered -- either genuinely none pulled, or a real regression"
      rc=1
    fi
  fi

  rm -rf "$scratch"
  return "$rc"
}

# The single "hi" session probe, against whatever server is already
# answering. Cheap, safe to run anywhere, and skips rather than fails
# when there is nothing to talk to -- which is why it belongs in `all`
# while `containers` below does not.
stage_client() {
  python3 scripts/e2e_session_probe.py
  return $?
}

# Any HTTP answer means up; only a connection-level failure means not --
# the same test entrypoint.sh's own wait loop uses.
server_answering() {
  python3 -c "
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen('$1/session', timeout=3)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
sys.exit(0)
" 2>/dev/null
}

# Full container lifecycle: build, start, probe, stop. This is the stage
# that makes the stack drivable without anyone building or launching it
# by hand.
#
# It cannot run in a cicd_runner worker, and not for want of a binary:
# a worker is started with no docker socket at all, by explicit design.
# Reaching it from an orchestrator means the coordinator side --
# container_control(relative_path, action) -- with this stage covering
# the same ground for a shell or a CI job that does have a daemon.
#
# A stack that was already up is left alone: the probe runs against it
# and nothing is stopped afterwards. Tearing down a server someone else
# started, as a side effect of a check, is not this stage's business.
stage_containers() {
  local rc=0 started_here=false

  if ! command -v docker >/dev/null 2>&1; then
    log "no docker in this environment (a cicd_runner worker has no socket by design) -- skipping"
    log "drive the lifecycle from the coordinator instead: container_control(relative_path, up|down|status)"
    return 2
  fi

  # Compose needs OPENCODE_GLOBAL_CONFIG resolved, and this stage can be
  # invoked as `bash tools/pipeline.sh containers` without the Makefile's
  # own default ever applying.
  # shellcheck source=/dev/null
  source scripts/lib/opencode-global-config.sh

  local port="${OPENCODE_SERVE_PORT:-49605}"
  local base_url="http://localhost:${port}"

  if server_answering "$base_url"; then
    log "a server is already answering at $base_url -- probing it, leaving it running"
  else
    started_here=true
    log "building the server image"
    bash scripts/compose.sh build server || return 1
    log "starting the server"
    bash scripts/compose.sh up -d server || return 1

    local waited=0
    until server_answering "$base_url"; do
      waited=$((waited + 3))
      if [ "$waited" -ge 120 ]; then
        log "server did not answer at $base_url within ${waited}s"
        bash scripts/compose.sh logs --tail 40 server
        bash scripts/compose.sh down
        return 1
      fi
      sleep 3
    done
    log "server answering after ${waited}s"
  fi

  OPENCODE_SERVER_URL="$base_url" python3 scripts/e2e_session_probe.py
  rc=$?
  case "$rc" in
    0) log "session probe: PASS" ;;
    2) log "session probe: SKIPPED (no provider/model selected)" ;;
    *) log "session probe: FAIL" ;;
  esac

  if [ "$started_here" = true ]; then
    log "stopping the stack this stage started"
    bash scripts/compose.sh down || rc=1
  fi

  # A skipped probe is not a failed lifecycle: the containers still
  # built, started, answered and stopped.
  [ "$rc" = 2 ] && rc=0
  return "$rc"
}

usage() {
  cat <<USAGE
usage: tools/pipeline.sh [lint|test|verify|e2e|client|containers|exec-bits|all]

  all         lint, test, verify, e2e, client
  client      one "hi" session against an already-running server
  containers  build, start, probe and stop the stack (needs docker)
  exec-bits   restore executable bits the Filesystem connector drops
              (also runs automatically before any other stage)
USAGE
}

main() {
  local target="${1:-all}"

  # Before anything else, and on every invocation. See
  # restore_exec_bits() for why this is not a step to be invoked.
  restore_exec_bits || true

  case "$target" in
    lint) run_stage lint ;;
    test) run_stage test ;;
    verify) run_stage verify ;;
    e2e) run_stage e2e ;;
    client) run_stage client ;;
    containers) run_stage containers ;;
    exec-bits) run_stage exec-bits ;;
    all)
      run_stage lint
      run_stage test
      run_stage verify
      run_stage e2e
      run_stage client
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
