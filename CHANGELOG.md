# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Tagged versions are created after merging to `main`, per
`docs/BRANCHING.md`'s delivery convention -- see that file's
"Versioning" section for the MAJOR/MINOR/PATCH rules applied here.

## [Unreleased]

## [0.1.0] - 2026-07-25

### Added

- **Server-side, provider-scoped session TTL** (`scripts/session_reaper.py`).
  opencode has no native session TTL/idle-expiry -- confirmed by source
  search across `packages/opencode/src/session/`. Runs as a background
  loop inside the `server` container, polling `GET /session` and calling
  `DELETE /session/{id}` on anything idle past its TTL (confirmed via
  source that this cancels in-flight work first, not just a bookkeeping
  delete). `local/ollama` sessions get an aggressive 10min TTL --
  sustained Ollama residency is the actual resource cost this cuts.
  Everything else gets a 60min fallback, kept above
  `run_eval_client.py`'s own 50min quota-wait threshold so it never
  preempts a legitimate cloud quota-retry. This is the safety net for a
  client that's abruptly gone (killed, crashed, container torn down) --
  see the abort-on-completion fix below for the case where the client is
  still alive.
- **`notebooks/examples/`** -- static, git-tracked reference notebooks
  for a basic OpenCode session and a basic Ollama native-API session.
  Replace three earlier AI-drafted notebooks that each guessed a
  different, wrong shape for opencode's undocumented HTTP API.
- Real, committed e2e test behind `run_eval_client.py`'s response-schema
  claim (`scripts/test_run_eval_client_e2e.py` +
  `scripts/tools/mock_openai_backend.py`) -- installs real
  `opencode-ai` via npm, runs `opencode serve` as a subprocess, drives
  it through this repo's actual client functions against a real
  SSE-emitting mock backend. Not yet observed passing end-to-end in
  every environment tried -- see Known Limitations.
- Quota/rate-limit exhaustion detected and reported distinctly (`Q`
  tier mark) instead of blocking indefinitely or risking a duplicate
  message. `quota_aware_send_message()` polls `GET /session/status`
  concurrently with the real request and aborts cleanly past
  `OPENCODE_QUOTA_WAIT_THRESHOLD_S` (default 3000s/50min) rather than
  re-POSTing to a session that might still be processing.
- Per-run server-side log capture (`server.log` alongside
  `report.json`), filtered by this run's own session IDs and error
  refs -- a raw whole-file copy would mix in every other run's
  interleaved lines on a persistent shared server.
- Live per-round-trip progress dots during a run, and per-tier HTTP
  errors (`E` mark) no longer crash the whole run.
- `git-workspace` role (`bash: allow`/`edit: allow`, isolated by
  mounting nothing but read-only `auth.json`) as a real place to run
  agentic/coding tasks -- not yet wired into the test ladder itself.
- `jupyter` role: persistent authoring server for hand-writing custom
  test notebooks, bind-mounted to the host (not a Docker named volume).

### Changed

- **Model selection moved from a Docker build arg to a runtime request
  parameter.** The original design baked `MODEL_PROVIDER`/`MODEL_ID` in
  at build time, one image layer per model. Once `opencode serve`'s
  HTTP API was confirmed to accept `providerID`/`modelID` directly in
  a request payload, the per-model build stopped buying anything.
- **Local Ollama models unified onto the same on-demand path as cloud.**
  Removed a `for_each` Terraform resource that was launching 5 real
  eval-client runs against Ollama in parallel on every plain "Deploy
  harness", regardless of `must_run = false` -- confirmed via a real
  deploy log. Local models now run through the exact same one-shot
  mechanism cloud models use.
- **Terraform: `ollama_base_url` split into `opencode_ollama_base_url`
  (keeps `/v1`) and `ollama_native_base_url` (no `/v1`).** One shared
  variable was feeding two consumers with genuinely incompatible URL
  shapes -- confirmed from Ollama's own source (`server/routes.go`):
  the OpenAI-compat surface (`/v1/*`) and the native API (`/api/*`)
  are two disjoint, hardcoded route trees; neither is derived from the
  other.
- **Dockerfile split into `server` (light) and `harness` (heavy,
  extends `server`) stages.** Every prior build failure traced back to
  dependencies the `server` role never actually used (spaCy,
  onnxruntime, click, PEP 668, a BuildKit `--chmod` requirement
  Cyberdyne's Docker doesn't support). `server` now only installs
  `ca-certificates` + `python3`.
- **`auth-data/auth.json` extraction now automatic under Terraform**
  (`data.external.auth_keys`, wrapping `scripts/extract-opencode-key.sh
  --all`) -- fixes a real failure where Docker silently creates an
  empty directory at a bind-mount source path that doesn't exist yet,
  producing an identical "credentials not found" error on every
  container that mounts the file.
- Static cloud-model matrix (`var.models` /
  `docker_container.eval[key]`, exactly 3 hardcoded entries, all 3
  broken or uncredentialed in practice) replaced with live discovery
  via `opencode models --verbose`.

### Fixed

- **Every tier was leaking its opencode session indefinitely.**
  `quota_aware_send_message()` already aborted on a quota-bailout and
  on any raw exception, but a tier that completed normally -- every
  PASS and every judged FAIL -- fell through both paths and was never
  aborted. For `local/ollama`, this kept the model persistently
  resident even after the eval-client process producing the load was
  gone. Confirmed via a before/after mock-server check: pre-fix, a
  2-tier run issued 0 abort calls; post-fix, exactly 2.
- `"eval-client: executable file not found"`, recurring on three
  independent paths -- root cause was Cyberdyne's Docker using the
  legacy builder (no working BuildKit/buildx), which didn't always
  propagate an inherited image `ENTRYPOINT` through multi-stage builds.
  Fixed at the source: the `harness` stage now redeclares `ENTRYPOINT`
  explicitly.
- Reruns against the same model were silently overwriting previous
  results in place. `run_eval_client.py` now rotates the existing
  results dir to a UTC-timestamped sibling before starting fresh.
- `browse_results()` picked up non-model directories as fake "models"
  on the Terraform path (model directories live one level deeper than
  on Compose). Now derives the model list from wherever `report.json`
  actually is, at any depth.
- Ollama cold-start: a fresh local/ollama run's first tier was timing
  out at exactly 300s with the server genuinely busy, not failing --
  matches Ollama's documented 5-minute idle-unload eating into the
  tier's own budget. Fixed with an explicit warm-up call before the
  real test ladder (`OPENCODE_WARMUP_TIMEOUT_S`, 600s default), plus
  explicit unload after the run finishes.

### Known Limitations

- **`scripts/test_run_eval_client_e2e.py` has not been observed
  passing cleanly in every environment tried.** In a network-restricted
  sandbox, `POST /session` hung indefinitely with the mock backend's
  request log staying completely empty -- points at an outbound call
  opencode itself makes during session creation, to a domain outside
  a restricted allowlist. On a machine with full network access
  (Cyberdyne), one of the two tests in the suite still failed the same
  way (`create_session` timing out at the same 20s bound) while the
  other passed -- suggesting something timing-sensitive rather than a
  hard categorical block. Root cause not yet confirmed; a live
  `strace`/`tcpdump` capture during the hang is the next real
  diagnostic step. This is a pre-existing test behavior, not something
  introduced by any patch delivered so far -- the test spawns
  `opencode serve` directly, bypassing this repo's own
  `entrypoint.sh`/`session_reaper.py` entirely.
- `extract_reply()`'s tool-call part shape is still an inference --
  the empirical test that confirmed the text-part response shape never
  triggered a tool call.
- No cost/latency capture -- `opencode stats` exists upstream but isn't
  wired into `run_eval_client.py` yet.
- Cloud eval runs are deliberately outside Terraform's state entirely
  (a plain `docker run` from a script, tracked in no `.tfstate`) -- see
  INSTALL.md for why this is a deliberate trade-off, not an oversight.
- Local Ollama models' `host.docker.internal` networking path is
  confirmed correct on paper (source-level) but has not been observed
  working end-to-end against a real Ollama instance from every
  environment this project has been developed in.
