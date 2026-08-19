# Install & Run

See [REQUIREMENTS.md](REQUIREMENTS.md) first, or run
`bash scripts/check-requirements.sh --all` to check your machine
automatically.

## Setup

**First, export `OPENCODE_GLOBAL_CONFIG`** so every container can find your
real opencode global config:

```bash
export OPENCODE_GLOBAL_CONFIG="$HOME/.config/opencode/opencode.json"
```

This project no longer maintains its own static list of local Ollama
models — `config/opencode.base.json` and `config/opencode.git-workspace.json`
both merge *under* your own global config (confirmed via opencode's source:
`config.ts`'s `loadGlobal()` loads first, the project's `OPENCODE_CONFIG`
overlays on top as an override, not a replacement). Your global config's
`provider["local/ollama"]["models"]` entries are what makes a given model
resolvable at all now. If you add a new local model (`ollama pull ...`),
add it there too — no project file needs editing. See REQUIREMENTS.md for
the exact format expected. Not set to a `~`-prefixed default in
`docker-compose.yml` deliberately — tilde expansion in a Compose volume
path is inconsistent across compose versions (confirmed via real
`docker/compose` issues #6506, #3872), so this needs to be a real
shell-expanded absolute path, exported before you run Compose or
`terraform apply`.

Scope credentials down to the one provider key each container actually
needs, rather than mounting your real (likely multi-provider) auth.json
wholesale:

```bash
bash scripts/extract-opencode-key.sh          # lists provider keys in your real auth.json
bash scripts/extract-opencode-key.sh --all    # writes auth-data/auth.json with every configured key
```

This runs on the host, outside both Compose and Terraform — key values
never get read into Terraform state or a Compose env file.

**If you're using the Terraform path**, this now happens automatically
on `plan`/`apply` via `data.external.auth_keys` (see CHANGELOG.md) —
it always extracts `--all` now (no fixed provider list to derive from
since `var.models` is gone, see "Known gaps" in README.md), so you
don't need to run the command above yourself unless you're on the
Compose-only path, or want to see what's available first. Either way,
key values never reach Terraform state.

**If you're using the Compose path**, run this first instead of
`extract-opencode-key.sh` directly:

```bash
bash scripts/ensure-auth-data.sh opencode deepseek zhipu
```

Compose has no precondition/
pre-flight hook mechanism on either CLI, unlike Terraform's `terraform_data` +
`precondition`, so this can't run automatically the way the Terraform
path's does — it's a script you run yourself, once, before
`make server-up` or an eval run. What it actually fixes: Docker's bind-mount
behavior silently creates an EMPTY DIRECTORY at `auth-data/auth.json`
if that path doesn't exist yet as a real file when a container first
mounts it — hit live during this project's own setup, every
`server`/`discover`/`eval` container failing identically with
"credentials not found" because the mount target was a phantom
directory, not a missing file. `ensure-auth-data.sh` detects that
exact case and clears it via `rmdir` (refuses on anything non-empty,
won't delete real content), then runs the real extraction — same
script, same effect as running `extract-opencode-key.sh` directly, plus
the one-time phantom-directory cleanup this specific failure mode needs.

The structured test ladder ships with this repo, populated:
`task-suite/test_ladder.json` — 9 categories, 25 tiers, escalating
difficulty within each category. See README.md's "Test ladder" section
for what's validated vs. new/unproven.

## Running

**Primary entry point** — `harness-control.sh`, a persistent tmux-based
menu (30% menu pane / 70% output pane):

```bash
bash harness-control.sh
```

Requires a real terminal and `tmux` (hard dependency, no fallback —
run it directly, not piped or from CI). All model/provider picking
happens in the menu pane itself via `scripts/lib/host-model-picker.sh`'s
host-side arrow-key/j-k picker — never inside a container, never a
nested prompt rendering in the output pane. Menu options: Deploy
harness / Remove harness (either backend, picked at the start of the
action) / Run an eval / View logs (past action/session logs) / Browse
results (`results/<model>/report.json` + per-category CVV output,
distinct from View logs) / Start Jupyter / Stop Jupyter (a persistent
authoring server for hand-writing custom-test notebooks — see
`config/opencode.git-workspace.json` and the `jupyter` service/Dockerfile
stage for what each of those is for) / Quit.

This wraps the same underlying scripts described below (`make tf-apply`,
`make server-up`, `select-and-run-eval.sh`,
`tf-select-and-run-eval.sh`) — it's a control surface over them, not a
separate mechanism. Everything from here down documents those
underlying scripts directly, for scripted/non-interactive use or if you
don't want the tmux UI.

**Quickest scripted path** — interactive picker, opencode `/models`-style,
wraps everything below into one step:

```bash
bash scripts/select-and-run-eval.sh                # menu: pick a number
bash scripts/select-and-run-eval.sh hy3             # skip the menu, run directly by name
bash scripts/select-and-run-eval.sh --dry-run hy3   # print the compose command, don't run it
```

Local Ollama options are derived live from `compose config
--services`, so this can't drift from the actual configured service
list. Cloud is a single `cloud` entry standing in for live discovery
via `opencode models --verbose` (the `discover` service) — not a fixed
list anymore; see README.md's "Known gaps" for why the earlier
per-model-hardcoded design (both here and in Terraform's now-deleted
`var.models`) was dropped. Picking which model to use happens entirely
on the HOST, not inside Docker: `discover` runs non-interactively
(`--list-json`, just returns the candidate list) when a real terminal
is attached, and `scripts/lib/host-model-picker.sh`'s arrow-key (or
j/k) menu runs on the host terminal to choose one — never a TTY
inside the container. Without a real terminal (CI, piped input),
`discover` falls back to its own unattended size-heuristic auto-pick
instead, same as before.

Terraform-provisioned infra has the equivalent single-step wrapper:

```bash
bash scripts/tf-select-and-run-eval.sh                       # live discovery (prompts if you have a terminal)
bash scripts/tf-select-and-run-eval.sh opencode/hy3-free       # skip discovery, run directly
bash scripts/tf-select-and-run-eval.sh --dry-run opencode/hy3-free  # print the docker commands, don't run
# equivalently: make tf-eval / make tf-eval MODEL=opencode/hy3-free / make tf-eval DRY_RUN=1
```

**Manual path**, same steps this script automates:

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

# Local Ollama models (same harness image and `eval` service as cloud --
# just set OPENCODE_MODEL_PROVIDER=local/ollama instead):
docker compose run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=gemma4:31b eval
docker compose run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=nemotron-3-nano:30b eval
docker compose run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=qwen3-coder:30b eval
docker compose run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=qwen3-coder-fixed:30b eval
docker compose run --rm -e OPENCODE_MODEL_PROVIDER=local/ollama -e OPENCODE_MODEL_ID=qwen2.5-coder:7b eval
# or just: bash scripts/select-and-run-eval.sh   (interactive picker, both cloud and local)
# Start Ollama on the host first, with OLLAMA_HOST=0.0.0.0:11434 (its
# default, loopback-only, is NOT reachable from the `server` container's
# host.docker.internal route -- this is `server`'s own reach-out to
# Ollama, unrelated to how the eval-client container above reaches
# `server` itself, which is the normal bridge network same as cloud).
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
bash ../scripts/tf-select-and-run-eval.sh   # live discovery (or MODEL=... / make tf-eval)
terraform destroy
```
Cloud eval runs aren't a Terraform resource (see README.md's "Known
gaps" for why) — `docker_image.harness` is what Terraform manages and
shares across the server, `discover`, and local Ollama containers; a
cloud eval run is a one-off `docker run` against that same image with
whatever provider/model was resolved.

## Results

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
Each category entry also carries a `progress_dots` string (`.` pass,
`F` fail, `R` needs review, `E` request/HTTP error, `Q` quota/rate-limit
exhausted) — same character sequence printed live to the console as
each tier runs, and again as an aligned grid at the end of the run. `E`
means the tier never got a real answer to score for a genuine
error (e.g. the model ID doesn't exist, a malformed response, a
connection failure) — treat it as "inconclusive," not a capability
failure the way `F` is. `Q` is a DIFFERENT kind of inconclusive: it
means opencode's own retry was still legitimately in progress
(confirmed via `GET /session/status`) when this harness gave up
waiting past `OPENCODE_QUOTA_WAIT_THRESHOLD_S` — nothing is wrong
with the model or the harness, the provider is just externally
throttled right now. `Q` tiers also carry `quota_wait_seconds` (how
much longer opencode's own next attempt would have needed) and
`status_events` (every status transition observed while waiting).

**Console output while a run is in progress:** each tier prints a
flushed, timestamped marker per HTTP round-trip (`[session:+0.3s]`,
`[setup:+12.1s]`, `[probe:+45.6s]` -- elapsed seconds since the tier
itself started, not wall-clock) as it happens, not just once the whole
tier finishes — a single slow LLM response previously looked identical
to a hung process from the CLI's perspective, and the timing itself
wasn't visible between request → result → next step. A run that's gone
silent for several minutes with the log file also showing no new lines
is the actual signal something's stuck; markers not advancing within a
single tier for a long time is expected for a slow model, not a bug --
compare the elapsed second count across tiers to actually see whether
a given step is unusually slow, instead of only knowing something
eventually completed.
