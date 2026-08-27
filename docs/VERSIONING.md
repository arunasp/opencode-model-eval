# Versioning

Semantic versioning (`MAJOR.MINOR.PATCH`) applied to the repo as a whole
via git tags. No package is published, so tags are the only version
artifact.

| Bump | Applies to |
|---|---|
| **MAJOR** | Breaking change to the environment contract: task-suite prompt format, results JSON schema, or required env vars — as of `v1.0.0`, `OPENCODE_MODEL_PROVIDER`, `OPENCODE_MODEL_ID` and the server URL |
| **MINOR** | Additive capability: a new model reachable through the global opencode config, a new results field, a new flag defaulting to prior behaviour |
| **PATCH** | Bug fixes, doc corrections, dependency or provider version bumps with no contract change |

The Dockerfile's `MODEL_PROVIDER` / `MODEL_ID` build args were once a
MAJOR-level contract. They no longer exist — model selection is a
runtime request parameter as of `v0.2.0`.

## Tagging

Tag `main` at the commit reaching a version boundary. Patch sets named
per `docs/BRANCHING.md` are named for the version they deliver **to**:
`opencode-model-eval-patch-0.2.0.tar.gz` lands at `v0.2.0`, so the tag
belongs on the commit that patch set produces.

## Tags

| Tag | Date | Contents |
|---|---|---|
| `v0.2.0` | 2026-07-25 | Shared harness image; Terraform and Compose deployment paths; scoped auth extraction, automatic under Terraform; the full test ladder; quota and rate-limit handling; per-run server log capture; `git-workspace` and `jupyter` roles; session TTL reaper; static example notebooks; split `README.md` / `INSTALL.md` / `CHANGELOG.md` / `REQUIREMENTS.md` |
| `v1.0.0` | 2026-07-27 | Breaking: `local/ollama` models come from the global opencode config rather than a static project list. Requires `OPENCODE_GLOBAL_CONFIG` |

`v0.1.0` was planned as a minimal checkpoint and never tagged; the repo
had moved well past that scope by the time a tag was cut. It stands as
the implicit baseline `v0.2.0` is additive on top of.

Not covered by any tag to date: agentic and tool-use tasks wired into
the ladder, cost and latency capture, N-runs and why-chain scoring. See
README's "Known gaps".
