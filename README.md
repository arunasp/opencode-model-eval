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
- [Checks and container lifecycle](#checks-and-container-lifecycle) — the staged pipeline
- [Changelog](CHANGELOG.md) — what's changed, fixed, and still unverified
- [Contributing](CONTRIBUTING.md)
- Governance: [`docs/CODEGEN.md`](docs/CODEGEN.md), [`docs/BRANCHING.md`](docs/BRANCHING.md),
  [`docs/VERSIONING.md`](docs/VERSIONING.md)

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

## Two deployment paths

Terraform and Docker Compose are both supported and maintained in
parallel. Neither is deprecated, and neither is the primary.
`harness-control.sh` asks "Which backend?" before every action (Deploy/
Remove harness, Run an eval, Jupyter start/stop) and drives the
equivalent command on whichever you pick:

| Action | Terraform | Docker Compose |
|---|---|---|
| Deploy harness | `make tf-apply` | `make server-up` |
| Remove harness | `make tf-destroy` | `make server-down` |
| Run an eval | `scripts/tf-select-and-run-eval.sh` | `scripts/select-and-run-eval.sh` |
| Jupyter up/down | `make tf-jupyter-up` / `-down` | `make jupyter-up` / `-down` |

Pick Terraform for plan/apply/destroy discipline and automatic
credential extraction on `plan`/`apply` (see INSTALL.md). Pick Compose
for a simpler, more direct path with no separate state file to manage.
Both build from the same `Dockerfile` and share `docker-compose.yml`'s
service definitions where Terraform's own resources don't need to
diverge from them. See [Known gaps](#known-gaps)
for the one deliberate asymmetry between them (cloud eval runs are
outside Terraform's state entirely, on both paths).

**Only one of them can be running at a time.** They create containers
with the same names and publish the same ports, so the choice is per
deployment, not per command -- renaming would move the failure from a
name conflict to a port conflict rather than remove it. Terraform plans
against its own state and cannot see a container Compose created, so
`docker_container.server` carries a precondition
(`scripts/tf-detect-container-conflicts.sh`) that fails the PLAN when
something already holds a managed name, instead of failing at create
time after the images are built. Containers Terraform creates are
labelled `managed-by=terraform` and `project=opencode-model-eval`, so
the check can say whether it found a Compose stack, a Terraform
leftover whose state was lost, or something unrelated, and tailor the
message. Adoption via `terraform import` is deliberately not offered:
it would leave Compose believing it still owns a container Terraform
will later destroy.

## Checks and container lifecycle

`tools/pipeline.sh` holds the staged checks, each also reachable as a
make target. A stage that exits 2 is reported as SKIPPED rather than
failed, which is how the same pipeline runs unchanged on a developer
machine, in a sandbox, and in a CI worker carrying different toolchains.

| Target | What it does |
|---|---|
| `make lint` | shellcheck, `py_compile`, pycodestyle, JSON and workflow parse |
| `make test` | every `scripts/test_*` suite, reporting how many executed and naming any that did not |
| `make verify` | repository invariants: the Compose resolver is the only call path, no host paths in tracked files, bind sources anchored to `HARNESS_ROOT`, no uncommitted file-mode changes |
| `make e2e` | discovery against a live Ollama; skips when none is reachable |
| `make client` | opens one session against a running server, sends `hi`, closes it, and writes the outcome to `results/e2e-session/`; skips when nothing answers |
| `make containers` | builds the image, starts the server, waits for it to answer, runs the client probe, then stops it again |
| `make exec-bits` | restores executable bits recorded in the index |
| `make ci` | lint, prose, test, verify, e2e and client |
| `make prose` | filler-word ratchet over the docs |
| `make deps` | dev dependencies into a project-local `.venv` |

`containers` is not part of `ci`: it starts and stops containers, which
is an action to ask for rather than a side effect of running checks. It
also leaves a stack it did not start running, so probing a server you
already have up does not tear it down underneath you.

### What a run tells you it wrote

Every run ends with an `ARTIFACTS WRITTEN` manifest naming its own log
file and size, plus any file a stage produced. The log carries the full
stdout and stderr of every stage -- tracebacks, warnings and skips that
the summary lines above it do not repeat -- and the manifest exists so
that reading it is not a step anyone has to remember. A stage that
declares an artifact which is not on disk has it listed as `MISSING`
rather than omitted, so a stage claiming something it did not produce
shows up rather than passing quietly.

The test stage reports its own skips the same way. The same `make test`
executes a different set of suites depending on what the environment
carries: a worker without `jq` skips the model-switch tests, an image
without Node skips the end-to-end suite, and a developer machine with
both runs everything. Without the count next to the verdict, the
greenest run is the one that tested least.

A container lifecycle stage needs a Docker daemon and so cannot run in
an unprivileged CI worker; it skips there and names the alternative.
An orchestrator holding the socket drives the same lifecycle through
Compose directly, which is why a bare `up` starts only `server` (see
[REQUIREMENTS.md](REQUIREMENTS.md#two-ways-compose-gets-invoked)).

## Test ladder

`task-suite/test_ladder.json` — 9 categories, 25 tiers total:

- `training_precedence_resistance`, `verification_depth_disclosure`,
  `self_correction_discipline` — tier 1-2 content seeded from prompts
  validated against Hy3 in a prior session.
- `fact_fabrication_resistance`, `reasoning`, `instruction_following`,
  `coding`, `failure_diagnostics_and_fixing`, `handling_contradictions`
  — new design, **unvalidated**. Every tier follows the same
  escalating-difficulty pattern but hasn't been run against any model
  yet. Expect wording calibration after the first runs against a model.

Escalation rule: run tier 1, escalate on pass, stop on first fail.
A category's ceiling is reported even on a tier-1 fail (ceiling = 0).

See [INSTALL.md](INSTALL.md#results) for how to read `report.json`.

## Known gaps

- **A scoring tool that fails to run no longer passes the tier.**
  Fixed. Tier criteria are `must_not_have_categories`, which an empty
  findings set satisfies trivially, so a `cvv_scan.py` that could not
  execute used to produce `findings: {}` and a PASS. The scanner now
  reports whether it ran, and a tier that was never scored is reported
  `SCAN_DID_NOT_RUN` with the reason. **What remains:** a tier whose
  criteria are purely negative still cannot distinguish a correct
  refutation from silence -- an empty reply satisfies
  `must_not_have` exactly as a good answer does. Tiers need something
  positive to pass on, which is a change to the ladder rather than the
  client.
- **The transcript now carries the evidence path.** Fixed. Tool calls
  live in the session's message chain rather than the final response,
  so a tier whose work spanned webfetch, grep and subagent dispatch
  used to produce a transcript with none of those markers -- CVV
  categories judging a claim made without a verification attempt were
  matched against text that structurally could not show one. The chain
  is now walked, child sessions included, and the calls appear in the
  transcript and in `tierN.raw.json`. **Known imprecision:** the calls
  are attributed to the setup turn as a group, because the chain does
  not cleanly partition by which prompt triggered which call.
- **Image attachments do not reach the model on the `local/ollama`
  path.** Not fixed -- upstream. The v1 API accepts a `FilePartInput`
  (`type`, `mime`, `url`)
  and the server takes it without complaint, but the model answers that
  it cannot see images. Confirmed by sending the same bytes down both
  paths with `scripts/vision_attachment_probe.py`: direct to Ollama's
  native `/api/chat` the model named all three colour bands in order;
  through an opencode session it reported no image support. This is
  upstream anomalyco/opencode#20802, which reports exactly this for
  custom OpenAI-compatible providers. Two further limits apply even
  where it works: opencode passes only text and image media to the
  model (PDF, AVIF, BMP, audio and video are accepted by clients and
  silently excluded), and Ollama's `/v1` surface takes only base64 data
  URLs for jpeg/jpg/png/webp, refusing http(s) URLs outright.
- **Agentic/tool-use tasks now have a path, but it isn't wired into the
  test ladder yet.** `server`/`eval`/`discover`/`local-ollama` still
  deny `edit`/`bash` outright (`opencode.base.json`) — fine for pure
  reasoning/knowledge tasks. The `git-workspace` role
  (`config/opencode.git-workspace.json`, `bash: allow`/`edit: allow`,
  made safe by mounting nothing but read-only `auth.json` rather than
  by narrowing the command set) is an isolated place to run
  agentic/coding tasks, but it's a standalone one-shot container
  (`make git-workspace` / `make tf-git-workspace`),
  not a `test_ladder.json` category yet — `coding`,
  `instruction_following`, and `failure_diagnostics_and_fixing` tiers
  still cannot exercise tool use through the structured run.
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
  container to start, not for it to be listening** —
  `entrypoint.sh`'s `eval-client` mode polls the server before running
  to cover this gap; if the server takes unusually long to come up,
  the 30-attempt/2-second poll (60s total) may need lengthening.
  Related and measured: an open port is not a ready server either — see
  `scripts/test_run_eval_client_e2e.py`'s `_wait_until_serving()`.
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
  will never tell you whether a cloud eval container is running —
  that's a trade-off, not a bug, and the same one Compose's own `eval`
  service already made.

See [CHANGELOG.md](CHANGELOG.md) for what's already been fixed and
what's still unverified in detail.
