# opencode-model-eval

Self-contained testing environment: a single, static, unmodified official
`opencode` image running `opencode serve`, plus a Python HTTP client that
drives a structured, escalating-difficulty test ladder against whatever
model you point it at. Model selection is a runtime request parameter
(`providerID`/`modelID` in the API payload), not a Docker build variant —
no per-model image builds, no rebuild cost for adding a new model. Scores
each response for verification discipline (fabrication, hedging honesty,
self-correction) rather than just capturing one-off terminal transcripts.

## Contents

- [Requirements](REQUIREMENTS.md) — what you need installed, and why
- [Install & Run](INSTALL.md) — setup, running the harness, reading results
- [Changelog](CHANGELOG.md) — what's changed, fixed, and still unverified
- [Contributing](CONTRIBUTING.md)
- Governance: [`docs/CODEGEN.md`](docs/CODEGEN.md), [`docs/BRANCHING.md`](docs/BRANCHING.md),
  [`docs/VERSIONING.md`](docs/VERSIONING.md) — same conventions as
  `opencode-plugin-ctx-squid`, scoped down to what this repo actually
  contains rather than copied wholesale

## Quick start

```bash
bash scripts/check-requirements.sh --all      # confirm your machine has what this needs
bash scripts/extract-opencode-key.sh --all    # scope credentials down from your real auth.json
bash harness-control.sh                       # primary entry point: tmux menu, deploy + run + browse results
```

See [INSTALL.md](INSTALL.md) for the full setup and every other way to
run this (scripted, manual, Terraform).

## Architecture

```
ghcr.io/anomalyco/opencode:<pinned-ref>   ← base: official, untouched
        │
        ▼
harness image (single, shared)             ← jq, python3, git, cvv_scan.py +
                                              axiom_cvv_verify.py, embedding
                                              model, entrypoint.sh dispatcher,
                                              run_eval_client.py,
                                              discover_and_select_model.py
        │
        ├── server container    entrypoint.sh serve         (persistent,
        │                       opencode serve --port 4096                one per
        │                       --hostname 0.0.0.0                        environment)
        │                       + session_reaper.py, backgrounded
        │
        ├── discover container  discover_and_select_model.py (one-shot,
        │                       (standalone CLI, no server needed)         no server dep)
        │
        └── eval container(s)   entrypoint.sh eval-client    (one-shot per
                                 → HTTP calls to server:4096                model under
                                                                            test, zero rebuild)
```

**Why this replaced an earlier per-model build design:** the original
version baked `MODEL_PROVIDER`/`MODEL_ID` in as Docker build args, one
thin image layer per model. Once `opencode serve`'s HTTP API was
confirmed to accept `providerID`/`modelID` directly in a request payload
(`server/routes/instance/httpapi/handlers/session.ts`, `session/prompt.ts`'s
`ModelRef` schema), the per-model build no longer bought anything —
model selection moved to a runtime parameter instead. See
[`docs/CODEGEN.md`](docs/CODEGEN.md)'s Docker section for the full
reasoning.

## Test ladder

`task-suite/test_ladder.json` — 9 categories, 25 tiers total:

- `training_precedence_resistance`, `verification_depth_disclosure`,
  `self_correction_discipline` — tier 1-2 content seeded from prompts
  actually validated against Hy3 in a prior session.
- `fact_fabrication_resistance`, `reasoning`, `instruction_following`,
  `coding`, `failure_diagnostics_and_fixing`, `handling_contradictions`
  — new design, **unvalidated**. Every tier follows the same
  escalating-difficulty pattern but hasn't been run against any model
  yet. Expect wording calibration after first real runs.

Escalation rule: run tier 1, escalate on pass, stop on first fail.
A category's ceiling is reported even on a tier-1 fail (ceiling = 0).

See [INSTALL.md](INSTALL.md#results) for how to read `report.json`.

## Known gaps / not yet handled by this harness

- **Agentic/tool-use tasks now have a path, but it isn't wired into the
  test ladder yet.** `server`/`eval`/`discover`/`local-ollama` still
  deny `edit`/`bash` outright (`opencode.base.json`) — fine for pure
  reasoning/knowledge tasks. The `git-workspace` role
  (`config/opencode.git-workspace.json`, `bash: allow`/`edit: allow`,
  made safe by mounting nothing but read-only `auth.json` rather than
  by narrowing the command set) is a real, isolated place to run
  agentic/coding tasks, but it's a standalone one-shot container
  (`docker-compose run --rm git-workspace` / `make tf-git-workspace`),
  not a `test_ladder.json` category yet — `coding`,
  `instruction_following`, and `failure_diagnostics_and_fixing` tiers
  still can't actually exercise real tool use through the structured
  run.
- **`extract_reply()`'s tool-call part shape is still an inference** —
  see CHANGELOG.md.
- **`manual_check` tiers require a human or a separate test run** —
  `coding`, `instruction_following`, and `failure_diagnostics_and_fixing`
  tiers can't be auto-passed by CVV scoring alone.
- **No cost/latency capture** — `opencode stats` exists upstream but
  isn't wired into `run_eval_client.py` yet.
- **Embedding model fetch needs GitHub reachable at build time** — see
  REQUIREMENTS.md.
- **Compose's `eval` service's `depends_on` only waits for the server
  container to start, not for it to actually be listening** —
  `entrypoint.sh`'s `eval-client` mode polls the server before running
  to cover this gap; if the server takes unusually long to come up,
  the 30-attempt/2-second poll (60s total) may need lengthening.
- **Cloud eval runs are deliberately outside Terraform's state
  entirely.** Terraform provisions the shared infra (server, network,
  volumes, image, `docker_container.discover`, local Ollama containers)
  but a cloud eval run itself — `make tf-eval` /
  `scripts/tf-select-and-run-eval.sh` — is a plain `docker run` from a
  script, tracked nowhere in `terraform.tfstate`. This replaced an
  earlier design with one static `docker_container.eval[key]` per
  hardcoded cloud model; that matrix covered exactly 3 entries, all 3
  broken or uncredentialed in practice, so a fixed list was actively
  worse than not tracking it at all. `terraform plan`/`terraform show`
  will never tell you whether a cloud eval container is currently
  running — that's a deliberate trade-off, not a bug, and the same one
  Compose's own `eval` service already made.

See [CHANGELOG.md](CHANGELOG.md) for what's already been fixed and
what's still unverified in detail.
