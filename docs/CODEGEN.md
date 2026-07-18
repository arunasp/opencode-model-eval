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
- **Python** (`scripts/*.py`, `scripts/tools/*.py`) — added when the CVV
  scoring layer (discovery, structured test ladder, cvv_scan.py,
  axiom_cvv_verify.py) was introduced. Section below, not
  pre-provisioned before it existed.
- **HCL** (`terraform/*.tf`)
- **JSON** (`config/opencode.base.json`, `task-suite/test_ladder.json`,
  generated result files)
- **YAML** (`docker-compose.yml`)
- **Dockerfile**

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

## Python

- Shebang: `#!/usr/bin/env python3`, invoked as `python3 script.py` —
  never `bash script.py`, never relying on the execute bit alone.
- PEP 8: 4-space indent, snake_case for variables/functions, PascalCase
  for classes, UPPER_SNAKE_CASE for constants, one file = one purpose.
- Type hints on function parameters and return types.
- Prefer the standard library over new dependencies. `cvv_scan.py` has
  zero third-party dependencies by design; `axiom_cvv_verify.py`'s
  spaCy/onnxruntime dependencies are explicitly optional, with graceful
  degradation to prior (already-validated) behavior if absent — new
  Python here should follow that same pattern rather than adding a hard
  dependency where a stdlib approach or an optional-with-fallback one
  would do.
- No stubs — either a function is implemented and tested against real
  input, or it doesn't exist yet. This repo's `cvv_scan.py` and
  `axiom_cvv_verify.py` both have a documented history of exactly this
  kind of unverified-on-real-input failure (regex working on a hand-
  simplified test case, breaking on the real one) — re-run against real
  data before calling anything done, not just the isolated unit test.
- No MQL5 section here, deliberately. This repo has no MQL5 in it —
  see the note at the top of this file about not copying unused
  language sections wholesale from a different project's CODEGEN.md.

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

- **Static server, not a build-per-model matrix.** Originally: immutable
  upstream base → shared/cached harness layer → thin per-model layer
  (`MODEL_PROVIDER`/`MODEL_ID` baked in as build args), one image per
  model. Replaced once `opencode serve`'s HTTP API was confirmed to
  accept `providerID`/`modelID` per request
  (`server/routes/instance/httpapi/handlers/session.ts`) — model
  selection is a runtime request parameter now, not a build variant.
  This isn't a violation of the old "don't collapse stages for
  convenience, cache-friendliness is the point" rule — it makes that
  rule's underlying concern (rebuild cost per model) moot, since there's
  no per-model build left to cache against. If a future change
  reintroduces a real need for model-specific image content, re-add the
  split then, with the same reasoning documented, not by default.
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
