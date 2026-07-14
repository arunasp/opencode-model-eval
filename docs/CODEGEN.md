# CODEGEN.md

Scoped to what this repo actually contains. This is **not** a copy of
`opencode-plugin-ctx-squid`'s CODEGEN.md — that project is a TypeScript
opencode plugin; this one is a Docker/Terraform test harness with no
TypeScript in it. Carrying over its language-specific sections would be
the same scope-creep mistake already caught once in that project
(unused Python/MQL5 sections pruned after being flagged) — not repeating
it here.

## Languages actually in use here

- **Bash** (`entrypoint.sh`, `scripts/*.sh`)
- **HCL** (`terraform/*.tf`)
- **JSON** (`config/opencode.base.json`, generated result files)
- **YAML** (`docker-compose.yml`)
- **Dockerfile**

If a future change introduces another language (a Python scoring layer
for the why-chain/N-runs work, most likely), add a section for it then —
don't pre-provision for it now.

## Bash

- Shebang: `#!/bin/bash`, invoked as `bash script.sh` — never `./script.sh`
  relying on the execute bit alone, never a mismatched interpreter.
- `set -euo pipefail` at the top of every script.
- Lint with `shellcheck` before considering a script done. Zero warnings,
  not just zero errors — `SC2155` (masked return values in
  `readonly x=$(...)`) has already been hit once in this repo and fixed;
  don't reintroduce it.
- Fail loudly with a message on stderr and a non-zero exit, never a
  silent empty/default output standing in for a real error.

## Terraform / HCL

- Provider versions pinned (`~> 4.5` for `kreuzwerker/docker`), not
  floating.
- `triggers` blocks content-hash the actual files that matter
  (`filesha1(...)`), not a manual version bump — so `terraform plan`
  only shows a rebuild diff when something real changed.
- `.terraform.lock.hcl` gets committed after the first real `terraform
  init` (pins the exact provider build for reproducibility). `.terraform/`,
  `*.tfstate`, `*.tfstate.backup`, and `*.tfvars` stay local and
  gitignored.
- No `terraform` binary or Docker daemon is available in the environment
  these files were authored in — every `.tf` file here was syntax-checked
  with a Python HCL2 parser, not `terraform validate`. Run `terraform
  init && terraform validate && terraform plan` yourself before trusting
  this beyond "parses."

## Docker

- Multi-stage builds split by mutability: immutable upstream base →
  shared/cached harness layer → thin per-model layer. Don't collapse
  these back into one stage for convenience; the cache-friendliness is
  the point.
- No secrets baked into any image layer. Auth is mounted at runtime only
  (see `scripts/extract-opencode-key.sh`), never `COPY`'d.

## General

- No stubs, no placeholder functions "to fill in later" — either a piece
  is built and tested, or it's explicitly documented as not-yet-built
  (see README's "Known gaps" section) rather than faked with a stub that
  looks done.
- Every generated artifact gets checked against what the project actually
  contains before being called finished — grep/view the real result, not
  just the reasoning that produced it.
