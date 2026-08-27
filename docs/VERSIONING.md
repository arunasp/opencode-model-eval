# VERSIONING.md

Semantic versioning (`MAJOR.MINOR.PATCH`) applied to the repo as a whole
via git tags — there's no published package here (unlike
`opencode-plugin-ctx-squid`, which ships an npm package), so tags are the
only version artifact.

- **MAJOR** — breaking change to the environment contract: task-suite
  prompt format, results JSON schema, or required env vars. The
  Dockerfile's old `MODEL_PROVIDER`/`MODEL_ID` build-arg interface used
  to be listed here; it no longer exists, since model selection moved
  to a runtime request parameter (see CHANGELOG's 0.2.0 entry). The
  equivalent contract now is `OPENCODE_MODEL_PROVIDER`/
  `OPENCODE_MODEL_ID` and the server URL, which the env-var clause
  already covers.
- **MINOR** — new capability that doesn't break existing usage: a new
  model reachable through your global opencode config, the
  N-runs/why-chain scoring layer landing on top of the current
  single-run execution substrate, a new results field that's additive,
  a new flag on `run_eval_client.py` that defaults to prior behaviour.
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
extraction + governance docs, no test suite content yet), but it was
never actually tagged at that point — the repo kept moving and the tag
kept getting deferred. Rather than tag the eventual first checkpoint
`v0.1.0` retroactively (which would have meant claiming a "minimal
checkpoint" tag for a state that already includes a full 9-category/
25-tier test ladder, quota handling, and several other features well
beyond that original scope), the actual first tag applies a MINOR bump
instead: `v0.1.0` stands as the implicit, never-tagged baseline this
batch's own new capabilities (the server-side session TTL reaper,
static example notebooks, `REQUIREMENTS.md` + `scripts/check-
requirements.sh`) are genuinely additive on top of, which is what a
MINOR bump means per this file's own rule above. `v0.2.0` is the real
first tag.

`v0.2.0` covers: the shared harness image, both Terraform and Compose
deployment paths, scoped auth extraction (now automatic under
Terraform), the full test ladder, quota/rate-limit handling, per-run
server log capture, the `git-workspace` and `jupyter` roles, the
session TTL reaper, static example notebooks, and the split
`README.md`/`INSTALL.md`/`CHANGELOG.md`/`REQUIREMENTS.md`
documentation. See `CHANGELOG.md` for the itemized history. Not part
of this tag: agentic/tool-use tasks wired into the test ladder itself,
cost/latency capture, N-runs/why-chain scoring — see README's "Known
gaps" section. Those remain `v0.2.0`-or-later territory.
