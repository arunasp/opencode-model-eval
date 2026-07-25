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

`v0.1.0` was planned early on to mean a minimal checkpoint (immutable
base + shared harness + per-model execution paths + scoped auth
extraction + governance docs, no test suite content yet) but was never
actually tagged at that point — the repo kept moving and the tag kept
getting deferred. By the time it's created, `v0.1.0` covers everything
up through the harness's current real state instead: the shared
harness image, both Terraform and Compose deployment paths, scoped
auth extraction (now automatic under Terraform), the full 9-category/
25-tier test ladder (`task-suite/test_ladder.json`), quota/rate-limit
handling, per-run server log capture, the `git-workspace` and `jupyter`
roles, the server-side session TTL reaper, static example notebooks,
and the split `README.md`/`INSTALL.md`/`CHANGELOG.md`/`REQUIREMENTS.md`
documentation. See `CHANGELOG.md` for the itemized history. Not part
of this tag: agentic/tool-use tasks wired into the test ladder itself,
cost/latency capture, N-runs/why-chain scoring — see README's "Known
gaps" section. Those remain `v0.2.0`-or-later territory.
