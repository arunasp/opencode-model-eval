# VERSIONING.md

Semantic versioning (`MAJOR.MINOR.PATCH`) applied to the repo as a whole
via git tags — there's no published package here (unlike
`opencode-plugin-ctx-squid`, which ships an npm package), so tags are the
only version artifact.

- **MAJOR** — breaking change to the environment contract: task-suite
  prompt format, results JSON schema, required env vars, or the
  Dockerfile's build-arg interface (`MODEL_PROVIDER`/`MODEL_ID`).
- **MINOR** — new capability that doesn't break existing usage: a new
  model added to `docker-compose.yml`/`terraform/variables.tf`, the
  N-runs/why-chain scoring layer landing on top of the current
  single-run execution substrate, a new results field that's additive.
- **PATCH** — bug fixes, doc corrections, dependency/provider version
  bumps with no contract change.

## What gets tagged

Tag `main` at the commit where a version boundary is reached. Patch sets
delivered per `BRANCHING.md`'s convention are named for the version they
deliver **to** (`opencode-model-eval-patch-0.2.0.tar.gz` lands you at
`v0.2.0`), so the tag should exist at the commit that patch set produces.

## Current state

`v0.1.0` — initial environment: immutable base + shared harness + 
per-model Docker/Terraform execution paths, scoped auth extraction,
governance docs. No task suite content, no N-runs/why-chain scoring yet
— see README's "Known gaps" section. Those are `v0.2.0`-or-later
territory, not part of this tag.
