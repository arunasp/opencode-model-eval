#!/usr/bin/env bash
# scripts/compose.sh -- run Docker Compose through whichever CLI this
# machine has, for the Makefile and anything else that cannot source a
# shell library. Resolution lives in scripts/lib/compose.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=/dev/null
source scripts/lib/compose.sh
compose_resolve
exec "${COMPOSE[@]}" "$@"
