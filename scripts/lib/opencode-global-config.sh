# scripts/lib/opencode-global-config.sh -- defaults OPENCODE_GLOBAL_CONFIG
# to the standard location if not already set. Sourced by
# harness-control.sh, select-and-run-eval.sh, and
# tf-select-and-run-eval.sh; not meant to be run standalone (no
# shebang/executable bit on purpose, matching
# scripts/lib/host-model-picker.sh's own convention).
#
# Aligns Compose/the host-side scripts with the Terraform path's own
# real default (var.opencode_global_config_path's pathexpand()).
# Deliberately real bash, not a Compose-file ${VAR:-${HOME}/...}
# default: nested variable interpolation in a default value is in the
# compose-spec, but per direct first-hand testimony (nickjanetakis.com's
# Docker Tip #98) it's a Compose v2 feature -- v1 "wasn't supported."
# This project's target machine runs docker-compose 1.29.2, the legacy
# v1 CLI, confirmed via `docker-compose --version`. A YAML-embedded
# nested default would be exactly as unreliable there as the tilde-
# expansion issue already avoided (real docker/compose issues #6506,
# #3872) for the same underlying reason. Real bash parameter expansion
# has no such version dependency.
export OPENCODE_GLOBAL_CONFIG="${OPENCODE_GLOBAL_CONFIG:-$HOME/.config/opencode/opencode.json}"
