# opencode-model-eval

Self-contained testing environment: an immutable, unmodified official
`opencode` image at the base, a shared test-harness layer on top of it,
and a thin per-model layer that carries only model identity. This exists
to run the same fixed task suite against multiple models (local and
cloud, via opencode as the common execution layer) and get artifact-backed,
diffable results per model rather than one-off terminal transcripts.

Governance: [`docs/CODEGEN.md`](docs/CODEGEN.md), [`docs/BRANCHING.md`](docs/BRANCHING.md),
[`docs/VERSIONING.md`](docs/VERSIONING.md) — same conventions as
`opencode-plugin-ctx-squid`, scoped down to what this repo actually
contains rather than copied wholesale.

## Layer structure

```
ghcr.io/anomalyco/opencode:<pinned-ref>   ← base: official, untouched
        │
        ▼
harness stage                              ← shared, cached: jq, entrypoint.sh,
                                              opencode.base.json, HOME pinned
        │
        ▼
model stage (built once per model)         ← thin: MODEL_PROVIDER / MODEL_ID
                                              env + labels only, no new files
```

Rebuilding for a new model only rebuilds the top (near-free) layer; the
base and harness layers stay cached and identical across every model
under test — that's the actual point of the three-stage split.

## Two equivalent execution paths

Same environment mechanisms as `opencode-plugin-ctx-squid`'s test
harness: Docker Compose for quick iteration, Terraform for anything you
want plan/apply/destroy discipline and content-hashed rebuild triggers
around. Both build from the same `Dockerfile` and mount the same three
things (`task-suite/prompts` read-only, scoped `auth-data/auth.json`
read-only, per-model `results/` read-write) — pick whichever fits the
moment, they're not meant to diverge.

```bash
# Compose path
docker compose build
docker compose up hy3 deepseek-v4-pro glm-5-2

# Terraform path
cd terraform
terraform init
terraform apply
$(terraform output -raw next_step)   # tails docker logs per model
terraform destroy                     # tears containers down
```

**One real seam, not smoothed over:** Terraform's `docker_container`
models a resource, not a one-shot `run --rm`. Here that actually fits —
each container is a batch job (entrypoint runs the suite, exits) rather
than the interactive shell case `ctx-squid-test-harness` had to work
around with `sleep infinity` + `docker exec`. `must_run = false` here
reflects that a stopped container is the *expected* end state, not
drift.

No `terraform` binary or Docker daemon available in the environment this
was authored in — every `.tf` file was syntax-checked with a Python HCL2
parser, not `terraform validate`/`plan`. Run those yourself before
trusting this beyond "parses." Same caveat applies to `docker compose
build` never having actually been run here.

## Before first run — things this Dockerfile could not verify from here

I don't have Docker/registry access in this environment, so the
following are **stated, not confirmed**, and should be checked once
against a real pull before you trust the pipeline:

1. **Registry path.** Verified 2026-07-14 against the official docs
   (`opencode.ai/docs/`): the current image is
   `ghcr.io/anomalyco/opencode`. The project was previously published as
   `ghcr.io/sst/opencode`, which now returns `401 UNAUTHORIZED` — it's
   been renamed, not just aliased. If pulls start failing, check
   `opencode.ai/docs/` again before assuming a local config problem.
2. **Digest pinning.** `OPENCODE_REF` defaults to `latest` in this repo,
   which is a placeholder, not a recommendation — `latest` is a mutable
   tag and defeats the "immutable base" goal. Resolve and pin it:
   ```bash
   docker pull ghcr.io/anomalyco/opencode:latest
   docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/anomalyco/opencode:latest
   # then set OPENCODE_REF=sha256:<digest-you-just-saw> in .env
   ```
3. **Base image distro.** The Dockerfile detects `apk` vs `apt-get` at
   build time rather than assuming one, since upstream has shipped both
   Debian and Alpine variants. If neither is found, the build fails
   loudly instead of silently guessing.
4. **Provider slugs in `docker-compose.yml`.** `opencode-zen/hy3`,
   `deepseek/deepseek-v4-pro`, and `zhipu/glm-5.2` are my best-guess
   provider/model identifiers, **not verified against a live
   `opencode models` listing**. Run `opencode models --refresh` inside a
   container built from this image and correct `MODEL_PROVIDER`/
   `MODEL_ID` in `docker-compose.yml` to match the actual listing before
   relying on results.

## Setup

Scope credentials down to the one provider key each container actually
needs, rather than mounting your real (likely multi-provider) auth.json
wholesale — same rationale and script as `opencode-plugin-ctx-squid`:

```bash
bash scripts/extract-opencode-key.sh                              # lists provider keys in your real auth.json
bash scripts/extract-opencode-key.sh opencode-zen deepseek zhipu   # writes auth-data/auth.json with exactly these 3
```

Pass every provider key your current `terraform/variables.tf` `models`
map (or the equivalent services in `docker-compose.yml`) actually needs —
this project's default matrix spans three providers (Hy3 via
opencode-zen, DeepSeek V4 Pro, GLM-5.2 via zhipu), and every container
mounts the same `auth-data/auth.json`, so a single-key file would only
authorize one of them. `gemma4-31b-local` needs no key (Ollama). Verified
end-to-end against a synthetic multi-provider file: listing, multi-key
extraction, single-key extraction, and the missing-key failure path
(fails loudly, writes nothing) all behave as documented.

This runs on the host, outside both Compose and Terraform — key values
never get read into Terraform state or a Compose env file.

Populate the fixed task suite (not included yet — this harness runs the
suite, it doesn't author it; see the separate task-suite drafting thread):

```bash
task-suite/prompts/
├── 01-reasoning-baseline.txt
├── 01-reasoning-paraphrase.txt
├── 01-reasoning-perturbed.txt
└── ...
```

One prompt per file, filename becomes the task ID in results.

## Running

```bash
docker compose build
docker compose up hy3 deepseek-v4-pro glm-5-2
# gemma4-31b-local uses network_mode: host to reach a local Ollama —
# start Ollama on the host first, and note host networking is
# Linux-only in Docker; adjust for Docker Desktop on macOS/Windows.
```

Results land in `results/<model>/`:
- `run-manifest.txt` — model string, `opencode --version`, run start time
- `<task>.json` — status, UTC timestamp, SHA-256 of the raw output, path
- `<task>.raw.txt` — the actual model output
- `<task>.stderr.log` — captured only if the task run failed

This matches the standing AI-to-AI relay artifact convention (plaintext +
SHA-256 + captured timestamp) so results can be relayed or diffed the
same way other artifacts in this project already are.

## Known gaps / not yet handled by this harness

- **Permissions are locked down** (`edit: deny`, `bash: deny` in
  `opencode.base.json`) — fine for pure reasoning/knowledge tasks, but
  this blocks agentic/tool-use capability tests (domain 6 from the
  capability taxonomy). A second config profile with permissions opened
  up is needed before running that category.
- **No parallel-run stability scoring yet** — the entrypoint runs each
  prompt file once. The N-runs-per-task and why-chain escalation logic
  discussed separately isn't wired in here; this is the execution
  substrate for it, not the scoring layer.
- **No cost/latency capture** — `opencode stats` exists upstream but
  isn't wired into the entrypoint yet.
