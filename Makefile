# Root-level entry point for common operations. Every target here
# delegates to a script/command that already exists elsewhere in this
# repo (scripts/select-and-run-eval.sh, docker-compose.yml,
# scripts/ensure-auth-data.sh, terraform/) -- this file adds no new
# logic, just a single, discoverable front door at the repo root
# instead of needing to know which script lives where.
#
# Usage:
#   make help                  -- list targets
#   make eval                  -- interactive model picker
#   make eval MODEL=hy3        -- run a specific target directly, no menu
#   make eval MODEL=hy3 DRY_RUN=1   -- print the command, don't run it
#   make build                 -- build all service images
#   make server-up             -- start the persistent opencode server
#   make server-down           -- stop everything
#   make server-logs           -- tail the server's own logs (opencode's
#                                  --print-logs output, mirrored to
#                                  docker logs -- see README caveat)
#   make auth                  -- list available provider keys (no args)
#   make auth KEYS="opencode deepseek"  -- extract these specific keys
#   make tf-init                -- terraform init (safe to re-run; cheap
#                                   once already initialized)
#   make tf-plan                 -- terraform plan (runs tf-init first)
#   make tf-apply                -- terraform apply -- interactive
#                                    confirmation by default, same as
#                                    running terraform directly; add
#                                    AUTO_APPROVE=1 to skip the prompt
#   make tf-destroy               -- terraform destroy -- same
#                                     confirmation behavior as tf-apply
#   make tf-output                -- terraform output (both next_step
#                                     and results_dirs)
#   make git-workspace             -- one-shot isolated git/bash/edit
#                                      workspace (compose run --rm)
#   make tf-git-workspace           -- same, Terraform side
#   make jupyter-up                 -- start the persistent Jupyter
#                                       authoring server
#   make jupyter-down               -- stop it
#   make jupyter-logs                -- tail it (auth token appears here
#                                        on first start)
#   make tf-jupyter-up               -- same, Terraform side
#   make tf-jupyter-down             -- same, Terraform side

.PHONY: help eval build server-up server-down server-logs auth \
        tf-init tf-plan tf-apply tf-destroy tf-output tf-eval \
        git-workspace tf-git-workspace jupyter-up jupyter-down jupyter-logs \
        tf-jupyter-up tf-jupyter-down \
        lint test verify e2e ci client containers exec-bits

help:
	@echo "make eval                          interactive model picker"
	@echo "make eval MODEL=hy3                run a specific target directly, no menu"
	@echo "make eval MODEL=hy3 DRY_RUN=1       print the command, don't run it"
	@echo "make build                         build all service images"
	@echo "make server-up                     start the persistent opencode server"
	@echo "make server-down                   stop everything"
	@echo "make server-logs                   tail the server's own logs"
	@echo "make auth                          list available provider keys"
	@echo "make auth KEYS=\"opencode deepseek\"  extract these specific keys"
	@echo "make tf-init                       terraform init"
	@echo "make tf-plan                       terraform plan (runs tf-init first)"
	@echo "make tf-apply                      terraform apply (interactive confirm)"
	@echo "make tf-apply AUTO_APPROVE=1        terraform apply -auto-approve"
	@echo "make tf-destroy                    terraform destroy (interactive confirm)"
	@echo "make tf-destroy AUTO_APPROVE=1      terraform destroy -auto-approve"
	@echo "make tf-output                     terraform output"
	@echo "make tf-eval                       cloud eval run (MODEL=provider/id, omit for live discovery, DRY_RUN=1 to preview)"
	@echo "make git-workspace                 one-shot isolated git/bash/edit workspace"
	@echo "make tf-git-workspace              same, Terraform side"
	@echo "make jupyter-up                    start the persistent Jupyter authoring server"
	@echo "make jupyter-down                  stop it"
	@echo "make jupyter-logs                  tail it (auth token appears here on first start)"
	@echo "make tf-jupyter-up                 same, Terraform side"
	@echo "make tf-jupyter-down               same, Terraform side"
	@echo "make lint                          shellcheck, py_compile, config parse"
	@echo "make test                          run every scripts/test_* suite"
	@echo "make verify                        assert the Compose resolver is the only call path"
	@echo "make e2e                           real end-to-end check against a live Ollama, skips if none reachable"
	@echo "make client                        one 'hi' session against a running server, skips if none answers"
	@echo "make containers                    build, start, probe and stop the stack (needs a docker daemon)"
	@echo "make exec-bits                     restore executable bits the Filesystem connector drops"
	@echo "make ci                            lint, test, verify, e2e and client in one run"

# Default OPENCODE_GLOBAL_CONFIG the same way the Terraform path already
# does (var.opencode_global_config_path's own default) -- ?= only takes
# effect if not already set from the environment or a prior assignment,
# and $(HOME) is Make's own reliable built-in, not dependent on
# Compose interpolation (nested defaults are a v2 feature, and this
# repo also runs against the v1 CLI, which lacks them).
OPENCODE_GLOBAL_CONFIG ?= $(HOME)/.config/opencode/opencode.json
export OPENCODE_GLOBAL_CONFIG

# The uid:gid every container runs as, so anything written to ./results
# or ./notebooks is owned by the invoking user rather than root. Same ?=
# and export treatment as OPENCODE_GLOBAL_CONFIG above, and the same
# limitation applies: a default that lives in one entry point is not a
# default. Anything driving compose directly (an orchestrator, CI, a
# bare `docker compose`) gets these from .env instead, and falls back to
# 1000:1000 in the compose file if neither is set.
HOST_UID ?= $(shell id -u)
HOST_GID ?= $(shell id -g)
export HOST_UID
export HOST_GID

# Compose is either the v2 `docker compose` subcommand or the v1
# `docker-compose` binary, and this repo has to run on machines
# carrying either. scripts/compose.sh picks whichever is present at
# call time and fails loudly when neither is.
COMPOSE := bash scripts/compose.sh

# Staged checks. Each delegates to tools/pipeline.sh, which reports every
# failing stage rather than stopping at the first, tees to logs/, and
# treats exit 2 as SKIPPED when a tool is missing from this environment.
lint:
	@bash tools/pipeline.sh lint

test:
	@bash tools/pipeline.sh test

verify:
	@bash tools/pipeline.sh verify

e2e:
	@bash tools/pipeline.sh e2e

client:
	@bash tools/pipeline.sh client

containers:
	@bash tools/pipeline.sh containers

# Runs on every pipeline invocation too -- named here as well because
# `make build` never goes near tools/pipeline.sh, and an image built from
# a script that lost its executable bit is the same defect one layer
# further along.
exec-bits:
	@bash tools/pipeline.sh exec-bits

ci:
	@bash tools/pipeline.sh all

eval:
	@bash scripts/select-and-run-eval.sh $(if $(DRY_RUN),--dry-run) $(MODEL)

build: exec-bits
	$(COMPOSE) build

server-up:
	$(COMPOSE) build
	$(COMPOSE) up -d server

server-down:
	$(COMPOSE) down

server-logs:
	@if docker inspect opencode-model-eval-server >/dev/null 2>&1; then \
		docker logs -f opencode-model-eval-server; \
	else \
		$(COMPOSE) logs -f server; \
	fi

jupyter-up:
	$(COMPOSE) build jupyter
	$(COMPOSE) up -d jupyter

jupyter-down:
	$(COMPOSE) stop jupyter

jupyter-logs:
	$(COMPOSE) logs -f jupyter

tf-jupyter-up: tf-init
	cd terraform && terraform apply -target=docker_container.jupyter -target=null_resource.jupyter_connect_info

tf-jupyter-down: tf-init
	cd terraform && terraform destroy -target=docker_container.jupyter

auth:
	@bash scripts/ensure-auth-data.sh $(KEYS)

git-workspace:
	$(COMPOSE) build git-workspace
	$(COMPOSE) run --rm git-workspace

tf-git-workspace: tf-init
	cd terraform && terraform apply -target=docker_container.git_workspace

tf-init:
	cd terraform && terraform init

tf-plan: tf-init
	cd terraform && terraform plan

# No -auto-approve by default -- same interactive confirmation you'd
# get running terraform directly, deliberately not skipped just
# because it's wrapped in a make target. AUTO_APPROVE=1 opts in
# explicitly for scripted/unattended use.
#
# Both depend on tf-init, matching tf-plan -- hit live: apply/destroy
# failing with terraform's own "Inconsistent dependency lock file"
# error (a provider required by the config with no version selected
# in the lock file) has no automatic recovery without this, since
# a plain `terraform init` (not even -upgrade) is what actually
# resolves that specific error class -- confirmed against Terraform's
# own documented dependency-installation behavior: a provider with no
# existing lock-file selection gets one added on init, no -upgrade
# needed for that case (-upgrade is only for re-resolving providers
# that already have a selection to a newer version).
tf-apply: tf-init
	cd terraform && terraform apply $(if $(AUTO_APPROVE),-auto-approve)

tf-destroy: tf-init
	cd terraform && terraform destroy $(if $(AUTO_APPROVE),-auto-approve)

tf-output:
	cd terraform && terraform output

# Cloud eval run against Terraform-provisioned infra (server/network/
# volume must already exist -- run tf-apply first). Mirrors the `eval`
# target above exactly: MODEL is optional (omit it for live discovery
# instead of a fixed matrix entry), DRY_RUN prints the resolved docker
# commands without running them.
tf-eval:
	@bash scripts/tf-select-and-run-eval.sh $(if $(DRY_RUN),--dry-run) $(MODEL)
