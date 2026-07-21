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
#   make build                 -- docker-compose build (all services)
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

.PHONY: help eval build server-up server-down server-logs auth \
        tf-init tf-plan tf-apply tf-destroy tf-output

help:
	@echo "make eval                          interactive model picker"
	@echo "make eval MODEL=hy3                run a specific target directly, no menu"
	@echo "make eval MODEL=hy3 DRY_RUN=1       print the command, don't run it"
	@echo "make build                         docker-compose build (all services)"
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

eval:
	@bash scripts/select-and-run-eval.sh $(if $(DRY_RUN),--dry-run) $(MODEL)

build:
	docker-compose build

server-up:
	docker-compose up -d server

server-down:
	docker-compose down

server-logs:
	docker-compose logs -f server

auth:
	@bash scripts/ensure-auth-data.sh $(KEYS)

tf-init:
	cd terraform && terraform init

tf-plan: tf-init
	cd terraform && terraform plan

# No -auto-approve by default -- same interactive confirmation you'd
# get running terraform directly, deliberately not skipped just
# because it's wrapped in a make target. AUTO_APPROVE=1 opts in
# explicitly for scripted/unattended use.
tf-apply:
	cd terraform && terraform apply $(if $(AUTO_APPROVE),-auto-approve)

tf-destroy:
	cd terraform && terraform destroy $(if $(AUTO_APPROVE),-auto-approve)

tf-output:
	cd terraform && terraform output
