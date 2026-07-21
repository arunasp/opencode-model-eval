# Root-level entry point for common operations. Every target here
# delegates to a script/command that already exists elsewhere in this
# repo (scripts/select-and-run-eval.sh, docker-compose.yml,
# scripts/ensure-auth-data.sh) -- this file adds no new logic, just a
# single, discoverable front door at the repo root instead of needing
# to know which script lives where.
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

.PHONY: help eval build server-up server-down server-logs auth

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
