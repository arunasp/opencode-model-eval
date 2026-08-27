# Code generation

Scoped to what this repo contains: a Docker and Terraform test harness.
No TypeScript, no MQL5. Language sections are added when a language
enters the repo, not pre-provisioned.

## Languages in use

| Language | Where |
|---|---|
| Bash | `entrypoint.sh`, `scripts/*.sh`, `tools/pipeline.sh` |
| Python | `scripts/*.py`, `scripts/tools/*.py` |
| HCL | `terraform/*.tf` |
| JSON | `config/*.json`, `task-suite/test_ladder.json`, results |
| YAML | `docker-compose.yml` |
| Dockerfile | `Dockerfile` |

## Bash

- Shebang `#!/bin/bash`, invoked as `bash script.sh`. Never
  `./script.sh` relying on the execute bit — the Filesystem connector
  drops it, which is why `make exec-bits` exists.
- `set -euo pipefail` at the top of every script.
- `shellcheck` clean before a script is done: zero warnings, not zero
  errors. `SC2155`, masked return values in `readonly x=$(...)`, has
  been hit here and fixed.
- Fail with a message on stderr and a non-zero exit. An empty or default
  output must never stand in for an error.

## Python

- Shebang `#!/usr/bin/env python3`, invoked as `python3 script.py`.
- PEP 8: 4-space indent, `snake_case` functions, `PascalCase` classes,
  `UPPER_SNAKE_CASE` constants, one file one purpose.
- Type hints on parameters and return types.
- Standard library over new dependencies. `cvv_scan.py`,
  `capture_proxy.py`, `hostnet.py` and `prose_check.py` have no
  third-party dependencies. `axiom_cvv_verify.py`'s spaCy and
  onnxruntime imports are optional, degrading to already-validated
  behaviour when absent — follow that pattern rather than adding a hard
  dependency.
- No stubs. A function is implemented and tested against production-like
  input, or it does not exist. Both scanners here have a history of the
  opposite failure: a regex that worked on a hand-simplified case and
  broke on the recorded transcript.

## Terraform and HCL

- Provider versions pinned (`~> 4.5` for `kreuzwerker/docker`).
- `triggers` blocks content-hash the files that matter via
  `filesha1(...)`, so `terraform plan` shows a rebuild only when
  something changed.
- Commit `.terraform.lock.hcl` after the first `terraform init`. Keep
  `.terraform/`, `*.tfstate`, `*.tfstate.backup` and `*.tfvars` local
  and gitignored.
- The `.tf` files here were syntax-checked with a Python HCL2 parser,
  not `terraform validate` — no terraform binary was available where
  they were written. Run `terraform init && terraform validate &&
  terraform plan` before trusting them beyond parsing.

## Docker

- **One static server, not a build-per-model matrix.** Model selection
  is a request parameter (`providerID`/`modelID`), confirmed in
  `server/routes/instance/httpapi/handlers/session.ts`. The earlier
  design baked `MODEL_PROVIDER`/`MODEL_ID` into a per-model layer; there
  is no per-model build left to cache. Reintroduce the split only if
  model-specific image content becomes necessary, with the reasoning
  recorded.
- No secrets in any image layer. Auth is mounted at runtime — see
  `scripts/extract-opencode-key.sh` — never `COPY`'d.

## General

- No stubs and no placeholder functions. A piece is built and tested, or
  it is recorded as unbuilt in README's "Known gaps".
- Check the artifact, not the reasoning that produced it. Read the file
  back, grep the result, inspect the tree.

## Prose

Documentation is linted by `make prose`
(`scripts/tools/prose_check.py`), which follows the Google developer
documentation style guide, CircleCI's docs style guide and the OpenStack
writing guidelines, plus rules ported from the `vale-ai-tells` and
`deslop` Vale packages for the structural tics of machine-written text.

Files carrying pre-existing hits are listed in the script's `BASELINE`
and may only improve; any other file must be clean. Suppress with
`<!-- prose-disable-file -->`, a `<!-- prose-disable -->` /
`<!-- prose-enable -->` section, `<!-- prose-disable-next-line -->`, or a
trailing `# noqa: prose`.
