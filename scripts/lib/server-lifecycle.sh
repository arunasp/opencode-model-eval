# scripts/lib/server-lifecycle.sh -- shared server-detection primitive.
# Sourced by harness-control.sh, select-and-run-eval.sh, and
# tf-select-and-run-eval.sh; not meant to be run standalone (no
# shebang/executable bit on purpose, matching
# scripts/lib/host-model-picker.sh and
# scripts/lib/opencode-global-config.sh's own convention).
#
# Extracted here after a real gap: existing_server_backend() first
# lived only in harness-control.sh's deploy() action, but
# select-and-run-eval.sh's `docker-compose run --rm eval` calls
# implicitly trigger the same server startup via Compose's own
# depends_on -- an entirely separate invocation path hitting the exact
# same port-bind collision, with zero awareness of the check. Confirmed
# live: FORCE_REDEPLOY=1 had no effect because this code path never
# went through deploy() at all. Same lesson already on file from an
# earlier incident (a wrapper script bypassing a Terraform-managed
# resource) -- fixing one canonical call site isn't enough by itself;
# every path that can bring the same resource up needs the same check.

# Returns the backend name currently holding OPENCODE_SERVE_PORT (or
# empty/exit 1 if nothing's reachable there). Same health-check style
# docker-compose.yml's own healthcheck already uses (python3, already
# a hard dependency, rather than adding curl just for this). Backends
# are distinguished by their confirmed, different container naming:
# Terraform's server has a fixed name (terraform/main.tf's
# docker_container.server: name = "opencode-model-eval-server", no
# index suffix); Compose (confirmed v1.29.2, the legacy CLI) names
# containers "<project>_<service>_<index>" with underscores --
# unambiguous against Terraform's hyphenated name.
existing_server_backend() {
  local port="${OPENCODE_SERVE_PORT:-49604}"
  python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://localhost:${port}/session', timeout=2)
except Exception:
    sys.exit(1)
" 2>/dev/null || return 1

  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "opencode-model-eval-server"; then
    echo "Terraform"
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^opencode-model-eval_server_"; then
    echo "Docker Compose"
  else
    echo "unknown backend"
  fi
}
