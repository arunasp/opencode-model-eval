# opencode-model-eval

Self-contained testing environment: a single, static, unmodified official
`opencode` image running `opencode serve`, plus a Python HTTP client that
drives a structured, escalating-difficulty test ladder against whatever
model you point it at. Model selection is a runtime request parameter
(`providerID`/`modelID` in the API payload), not a Docker build variant —
no per-model image builds, no rebuild cost for adding a new model. Scores
each response for verification discipline (fabrication, hedging honesty,
self-correction) rather than just capturing one-off terminal transcripts.

Governance: [`docs/CODEGEN.md`](docs/CODEGEN.md), [`docs/BRANCHING.md`](docs/BRANCHING.md),
[`docs/VERSIONING.md`](docs/VERSIONING.md) — same conventions as
`opencode-plugin-ctx-squid`, scoped down to what this repo actually
contains rather than copied wholesale.

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
`docs/CODEGEN.md`'s Docker section for the full reasoning, including why
this isn't a violation of the project's own "don't collapse build stages
for convenience" rule (it makes that rule's underlying concern —
rebuild cost per model — moot, since there's no per-model build left).

## Before first run — things this repo could not verify from here

I don't have Docker/registry/network access in this environment, so the
following are **stated, not confirmed**, and should be checked once
against a real run before you trust the pipeline:

1. **Registry path.** Verified 2026-07-14 against the official docs
   (`opencode.ai/docs/`): the current image is `ghcr.io/anomalyco/opencode`.
   The project was previously published as `ghcr.io/sst/opencode`, which
   now returns `401 UNAUTHORIZED` — it's been renamed, not just aliased.
2. **Digest pinning.** `OPENCODE_REF` defaults to `latest` — a
   placeholder, not a recommendation. Resolve and pin it:
   ```bash
   docker pull ghcr.io/anomalyco/opencode:latest
   docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/anomalyco/opencode:latest
   # then set OPENCODE_REF=sha256:<digest-you-just-saw> in .env
   ```
3. **`opencode serve`'s request/response schema.** Both sides are now
   confirmed, not guessed. Request: sourced directly from real code
   (`POST /session` per `Session.CreateInput`, `POST /session/{id}/message`
   per `PromptInput`/`ModelRef`). Response: confirmed **empirically** —
   ran a real `opencode serve` instance (`opencode-ai@1.18.3` via npm)
   against a mock OpenAI-compatible backend under full control and
   captured the actual reply shape: `{"info": {...}, "parts": [{"type":
   "text", "text": "..."}, ...]}`. `run_eval_client.py`'s
   `extract_reply()` matches this exactly. One thing the empirical test
   also caught: opencode requests `stream: true` from the backend — a
   mock that answers with flat synchronous JSON (not real SSE chunks)
   silently produces a response with no text part at all, no error.
   **Tool-call part shape specifically was not exercised** by this test
   (the mock never triggered a tool call) — that branch of
   `extract_reply()` remains an inference, not empirically confirmed.
   Also observed, worth knowing for request-count/cost expectations:
   opencode fires an extra background title-generation call before the
   real one, per session.
4. **Provider slugs.** `hy3` is **verified** — corrected from
   `opencode-zen/hy3` to `opencode/hy3-free` against the live OpenCode
   Zen API in a prior session (the docs page omitted `hy3-free` while
   the live API listed it). `deepseek/deepseek-v4-pro` and
   `zhipu/glm-5.2` remain best-guess identifiers — use
   `scripts/discover_and_select_model.py` to resolve a working
   provider/model automatically rather than trusting the guesses.
5. **`docker compose build`/`terraform apply` never actually run here.**
   Python files: syntax-checked (`py_compile`) and unit-tested against
   synthetic inputs matching confirmed real schemas. Shell: `shellcheck`
   (zero warnings). YAML/JSON: parsed. Terraform: HCL2-parsed (no
   `terraform` binary available). None of this substitutes for an actual
   build/run — do that before trusting this beyond "the pieces are
   internally consistent."
6. **Local Ollama models' networking path is unverified end-to-end.**
   The `server` container reaches host-run Ollama via
   `host.docker.internal:host-gateway` (bridge networking, added
   2026-07-20) -- this only works if Ollama is bound to `0.0.0.0`, not
   its default `127.0.0.1`-only loopback bind. Start Ollama with
   `OLLAMA_HOST=0.0.0.0:11434` first. `config/opencode.base.json`'s
   `provider["local/ollama"]` block is now a FALLBACK, not the source
   of truth -- see #7.
7. **Model auto-discovery vs. the explicit eval-target list -- two
   different things, don't conflate them.** At `server` startup,
   `discover_local_ollama_models.py` queries Ollama's native
   `/api/tags` and regenerates `provider["local/ollama"].models` so
   opencode knows about whatever's actually installed on the host
   right now -- fixes config staleness (a model pulled/removed on
   Cyberdyne shows up without editing JSON). It does NOT decide which
   models the eval containers below (`gemma4-local`,
   `qwen3-coder-local`, etc.) actually run the test suite against --
   that's still the explicit `docker-compose.yml`/`terraform` service
   list, unaffected by discovery. Add a sixth Ollama model on the
   host and the provider auto-discovers it (opencode could route to
   it manually), but you still need a new eval-client service entry
   to run the test ladder against it automatically. Discovery
   degrades gracefully: if Ollama's unreachable at server startup,
   the baked-in static 5-model list is used unchanged, not a hard
   failure -- tested against a real local HTTP server standing in for
   Ollama (both the success and unreachable paths), not just assumed.
   NOT tested: the real container startup path (entrypoint.sh calling
   this at `serve` time) -- no Docker to run that here.
8. **`run_eval_client.py`'s response-schema claim now has a real,
   committed e2e test behind it** (`scripts/test_run_eval_client_e2e.py`
   + `scripts/tools/mock_openai_backend.py`) -- previously this was a
   prior session's one-off finding, asserted in a docstring with no
   re-runnable artifact. The test installs real `opencode-ai@1.18.3`
   via npm, runs `opencode serve` as a subprocess, and drives it
   through the project's actual `create_session`/`send_message`/
   `extract_reply()` functions against a real (if minimal) SSE-emitting
   backend. **Could not get a passing run in this environment**: `POST
   /session` hangs indefinitely and the mock backend's request log
   stays completely empty, meaning opencode never reaches the
   configured provider at all -- points at an outbound network call
   opencode itself makes during session creation (telemetry/update-
   check, unconfirmed which) to a domain outside this sandbox's
   restricted allowlist (`opencode.ai` is not in it), with no observed
   fast-fail/offline mode. Isolated by testing the mock backend
   directly, bypassing opencode entirely -- confirmed correct on its
   own (valid `/v1/models`, valid SSE stream). Run this test on
   Cyberdyne (unrestricted network) where it should actually pass; if
   it still hangs there, the cause is something other than sandbox
   egress restrictions and needs fresh diagnosis.
9. **The Dockerfile is now two stages: `server` (light) and `harness`
   (heavy, extends `server`).** This directly resolves caveats #5 and
   #6's build failures for the `server` role specifically -- every
   failure traced during development (spaCy/onnxruntime/click/PEP 668/
   BuildKit `--chmod`) came from dependencies the `server` role never
   actually used. `server` now only installs `ca-certificates` +
   `python3` (no pip packages at all); `harness` extends it with
   `py3-pip`/`git`/spaCy/onnxruntime/click for the CVV scoring layer
   that only `eval`/`discover`/`local_ollama` need.
   `docker-compose.yml`'s `server` service builds with `target: server`
   explicitly; every other service still builds the default (last)
   stage, `harness`, unchanged. Terraform equivalent:
   `docker_image.server` (new, light) vs. `docker_image.harness`
   (unchanged, heavy) -- `docker_container.server` now references the
   former. Verified: `bash -n` on all 9 RUN blocks across both stages,
   and `entrypoint.sh`'s full serve-mode dispatch logic actually
   executed end-to-end under `dash` with a stubbed `opencode` binary
   (not just syntax-checked) -- confirmed the discovery-failure
   fallback path and the final `exec opencode serve --port ...
   --hostname ...` call both fire correctly under POSIX sh. NOT
   verified: an actual `docker-compose build server` against the new
   light target (no Docker daemon here) -- this is the next real test
   to run.
10. **`auth-data/auth.json` is now auto-extracted by Terraform, with a
    fail-fast check backing it up.** Hit live on Cyberdyne: Docker
    silently creates an empty directory at a bind-mount source path
    that doesn't exist yet rather than erroring, so every container
    that mounts this file (`server`, `discover`, `eval` x3,
    `local_ollama` x5 -- all 12) hit an identical "credentials not
    found" failure the first time this ran. `data.external.auth_keys`
    (terraform/main.tf) now runs `scripts/tf-extract-auth-keys.sh` --
    a wrapper around `scripts/extract-opencode-key.sh` -- automatically
    on `plan`/`apply`, deriving the needed provider list from
    `var.models` rather than a second hardcoded copy. **Security
    property, verified against `hashicorp/external`'s own docs, not
    assumed:** "All output values are stored in the Terraform state
    file" -- so the wrapper is deliberately designed to print ONLY a
    `{"status":"ok","keys_extracted":"..."}` confirmation to stdout,
    never real key material; the actual secret write happens entirely
    inside `extract-opencode-key.sh`, writing straight to
    `auth-data/auth.json` on disk, never read back into a Terraform
    value. Verified end-to-end in a sandbox (fake source `auth.json`,
    real script invocation, confirmed stdout contains no secrets while
    the real file on disk does) and the `distinct([for m in var.models
    : m.provider])` expression tested in isolation against real
    `tofu apply` output, not assumed from HCL familiarity.
    `terraform_data.auth_file_check`'s `fileexists()`-based
    precondition (documented to hard-error, not return false, if the
    path is a directory -- the exact phantom-directory bug) stays as a
    defense-in-depth backstop after extraction, `depends_on`'d by all
    four container resource types. NOT verified: a real
    `terraform apply` against this exact flow on Cyberdyne (would
    need real opencode credentials present, which this sandbox
    doesn't have) -- the manual `bash scripts/extract-opencode-key.sh`
    path documented in Setup below still works unchanged if you'd
    rather not have Terraform run it automatically.

## Setup

Scope credentials down to the one provider key each container actually
needs, rather than mounting your real (likely multi-provider) auth.json
wholesale:

```bash
bash scripts/extract-opencode-key.sh                              # lists provider keys in your real auth.json
bash scripts/extract-opencode-key.sh opencode deepseek zhipu      # writes auth-data/auth.json with exactly these 3
```

This runs on the host, outside both Compose and Terraform — key values
never get read into Terraform state or a Compose env file.

**If you're using the Terraform path**, this now happens automatically
on `plan`/`apply` via `data.external.auth_keys` (see README caveat
#10) — the provider list comes from `var.models`, so you don't need to
run the command above yourself unless you're on the Compose-only path,
or want to see what's available first. Either way, key values never
reach Terraform state.

**If you're using the Compose path**, run this first instead of
`extract-opencode-key.sh` directly:

```bash
bash scripts/ensure-auth-data.sh opencode deepseek zhipu
```

docker-compose (v1.29.2 confirmed, the legacy CLI) has no precondition/
pre-flight hook mechanism, unlike Terraform's `terraform_data` +
`precondition`, so this can't run automatically the way the Terraform
path's does -- it's a script you run yourself, once, before
`docker-compose up`/`run`. What it actually fixes: Docker's bind-mount
behavior silently creates an EMPTY DIRECTORY at `auth-data/auth.json`
if that path doesn't exist yet as a real file when a container first
mounts it -- hit live during this project's own setup, every
`server`/`discover`/`eval` container failing identically with
"credentials not found" because the mount target was a phantom
directory, not a missing file. `ensure-auth-data.sh` detects that
exact case and clears it via `rmdir` (refuses on anything non-empty,
won't delete real content), then runs the real extraction -- same
script, same effect as running `extract-opencode-key.sh` directly, plus
the one-time phantom-directory cleanup this specific failure mode needs.

The structured test ladder ships with this repo, populated:
`task-suite/test_ladder.json` — 9 categories, 25 tiers, escalating
difficulty within each category. See `## Test ladder` below for what's
validated vs. new/unproven.

## Running

```bash
# 1. Start the server -- one, persistent, shared across every model under test
docker compose up -d server
# waits for a healthy state (see docker-compose.yml healthcheck) before
# anything else proceeds

# 2. Resolve a model
docker compose run --rm discover
# writes results/discovered-model.env, e.g.:
#   OPENCODE_MODEL_PROVIDER=groq
#   OPENCODE_MODEL_ID=llama-4-70b
# — or select directly, skipping discovery:
docker compose run --rm discover python3 /usr/local/bin/discover_and_select_model.py --model opencode/hy3-free

# 3. Run the eval suite against whatever was resolved
export $(cat results/discovered-model.env | xargs)
docker compose run --rm eval

# Local Ollama models (all five share the same harness image):
docker compose run --rm gemma4-local
docker compose run --rm nemotron-3-nano-local
docker compose run --rm qwen3-coder-local
docker compose run --rm qwen3-coder-fixed-local
docker compose run --rm qwen2.5-coder-local
# network_mode: host, Linux only; start Ollama on the host first, with
# OLLAMA_HOST=0.0.0.0:11434 (its default, loopback-only, is NOT reachable
# from the server container's host.docker.internal route). Unverified
# beyond "resolves on paper" -- no Docker/Ollama access in the
# environment this was authored in; confirm end-to-end on real hardware
# before trusting results from these five.
```

`discover_and_select_model.py`'s free-tier heuristic (cost field absent
or all-zero = free) is inferred from opencode's confirmed config schema
but hasn't been spot-checked against real provider output — if selection
looks wrong, run `opencode models --verbose` directly and inspect the
actual `cost` shape before trusting the filter blindly.

**Terraform path** (plan/apply/destroy discipline, content-hashed rebuild
triggers):
```bash
cd terraform
terraform init
terraform apply
$(terraform output -raw next_step)   # tails server + each model's eval run
terraform destroy
```
Every `docker_container.eval[model]` shares the same `docker_image.harness`
now — adding a model to `var.models` costs zero rebuild, just a new
container with different runtime env vars.

Results land in `results/<provider_modelid>/`, structured per
category/tier rather than a single flat manifest:

```
results/<provider_modelid>/
├── report.json                   # per-category ceiling summary, top-level
└── <category>/
    ├── tier1.json                 # passed/needs_manual_review/reason/findings
    ├── tier1.transcript.md        # ## User / ## Assistant transcript, cvv_scan.py-parseable
    ├── tier1.raw.json             # full request/response JSON, both turns
    ├── tier2.json                 # only present if tier 1 passed (escalation stopped otherwise)
    └── ...
```

**Reading `report.json`:** each category's `ceiling` is the highest tier
passed. A category stopping at tier 1 isn't necessarily a capability
failure — check the corresponding `tier1.json`'s `reason` field:
`needs_manual_review: true` means no CVV violation fired but the tier
requires human/test confirmation (format compliance, code correctness)
that this harness can't check automatically. Don't read a
`needs_manual_review` stop as the same thing as an actual CVV violation.

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

## Known gaps / not yet handled by this harness

- **Permissions are locked down** (`edit: deny`, `bash: deny` in
  `opencode.base.json`) — fine for pure reasoning/knowledge tasks, but
  this blocks agentic/tool-use capability tests. A second config profile
  with permissions opened up is needed before running that category.
- **`extract_reply()`'s tool-call part shape is still an inference** —
  the empirical test that confirmed the text-part response shape never
  triggered a tool call, so that specific branch (`"tool" in
  ptype.lower()`) hasn't been checked against real output. If a
  transcript is missing tool-call detail, check this first.
- **`manual_check` tiers require a human or a separate test run** —
  `coding`, `instruction_following`, and `failure_diagnostics_and_fixing`
  tiers can't be auto-passed by CVV scoring alone. See the `report.json`
  reading note above.
- **No cost/latency capture** — `opencode stats` exists upstream but
  isn't wired into `run_eval_client.py` yet.
- **Embedding model fetch needs GitHub reachable at build time.**
  `scripts/fetch_embedding_model.sh` clones a GitHub repo with the ONNX
  weights committed in-repo (deliberately avoids a Hugging Face/Ollama
  dependency) — if your build environment only allowlists package
  registries and not `github.com`, this step will fail.
- **`docker_container.eval`'s and Compose's `eval` service's `depends_on`
  only waits for the server container to start, not for it to actually
  be listening** — `entrypoint.sh`'s `eval-client` mode polls the server
  before running to cover this gap; if the server takes unusually long to
  come up, the 30-attempt/2-second poll (60s total) may need lengthening.
