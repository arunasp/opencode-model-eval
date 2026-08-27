# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Tagged versions are created after merging to `main`, per
`docs/BRANCHING.md`. The MAJOR/MINOR/PATCH rules applied here are in
`docs/VERSIONING.md`.

## [1.1.0] - 2026-08-27

### Added

- The test stage prints each test's name beside its result and fails on
  any `ResourceWarning`. A suite that exercises failure paths prints text
  indistinguishable from an unplanned fault, so a defect leaking through
  is invisible among the deliberate ones; naming every line's assertion
  and failing on unowned warnings separates them by construction. Not
  `-W error::ResourceWarning`, which cannot work here: the warning is
  raised from `__del__` during garbage collection and an exception in a
  finalizer cannot propagate.

- `.github/workflows/ci.yml` runs the same `make` targets on every push
  to `main` and every pull request, in four jobs: `checks` (lint, prose,
  verify), `test`, `e2e-mock` and `containers-mock`. No model is called
  from any of them, so CI works on a fork with no secrets. The
  mock-backed jobs assert their stage did not SKIP, since a skipped
  stage exits 0 and would otherwise report a skip as a pass.
- `scripts/tools/workflow_check.py`, run by `make lint`: parses the
  workflows, checks heredocs inside `run:` blocks terminate at column 0,
  and compiles Python heredoc bodies. A malformed workflow does not fail
  loudly — GitHub declines to run it and the repository quietly stops
  having CI.
- `scripts/tools/mock_openai_backend.py` serves Ollama's native
  `/api/tags` alongside the OpenAI routes, and gained a `__main__` that
  prints its bound port on the first stdout line. It was a library
  exposing only `make_server()`; a CI job needs it alive across separate
  `make` invocations, and `--port 0` with the port read back avoids
  guessing one.

- `scripts/tools/capture_proxy.py` records the resolved system prompt
  and tool definitions sent to the provider. opencode assembles both per
  request and does not persist them, so the outbound request is the only
  place they exist. Off unless `OPENCODE_CAPTURE_PROXY` is set. Parses
  the request, relays the response byte for byte, redacts credentials
  before write. Plain HTTP is fully visible; HTTPS arrives as CONNECT
  with an opaque body, and each record states which.
- `scripts/trace_session_hang.py` enumerates session-creation
  hypotheses, one variant each, with egress sampled during the hang.
- `entrypoint.sh` runs `opencode models --refresh` before `serve`, gated
  on `OPENCODE_WARM_CATALOG`, timeout-bounded and non-fatal. Moves 191
  npm connections out of the first request's path.
- `docker-compose.yml` gains a healthchecked `capture` service; `server`
  depends on it.
- `.env.example` documents `OPENCODE_CAPTURE_PROXY` and
  `OPENCODE_CAPTURE_NO_PROXY`.


- **Every pipeline run names the files it wrote.** A tool invocation
  returns stdout and stderr; the log the run tee'd everything to was
  never named there, so opening it was a step the reader had to
  remember. In practice that step gets skipped, and a verdict read from
  stdout can disagree with the log on disk -- which carried tracebacks,
  resource warnings, and a suite skipping itself for a missing
  dependency. Each run now ends with an `ARTIFACTS WRITTEN` manifest
  listing its own log path and size plus any file a stage declared
  through `record_artifact`, printed where the caller cannot avoid it. A
  declared artifact that is not on disk is listed as `MISSING` rather
  than omitted.
- **The test stage reports what it did not run.** The same `make test`
  executes a different set of suites in each environment: the
  cicd-runner worker has no `jq`, the harness image has neither `jq` nor
  Node, a developer machine has both. Skips were single lines in the
  middle of the output, so a run that tested less looked identical to
  one that tested more. The stage now counts executed suites, names
  skipped ones with the reason, and puts both next to the verdict.
  Suites that skip internally (a `unittest` run reporting `skipped=`,
  which exits zero) are counted too.

- **`run_eval_client.py` has a command line.** It previously read no
  argv at all, so `--help` silently executed the entire 25-tier ladder.
  `--categories` and `--tiers` take ids, 1-based positions or ranges;
  `--list` prints the ladder without needing a model configured;
  `--help` behaves. A narrowed run announces itself as PARTIAL in its
  own log and records `tiers_available`, so a ceiling bounded by the
  selection cannot later read as a model limit. Against a slow local
  model this is the difference between an instrument and an all-night
  job.
- **Live session progress while a tier runs.** The wait loop printed
  only on a status transition, and its one heartbeat was gated on
  status being `retry` -- a long tier sits at `busy`, so a tier doing
  work and a client wedged on a dead socket produced identical
  logs (nothing) until one of them stopped. A 221s tier logged one line
  and then silence. The heartbeat now fires on any unchanged status and
  carries what the session has done: messages, steps, tool calls with
  the last tool and its state, characters of text and reasoning, token
  totals. It follows subagent child sessions, without which a `task`
  dispatch looks identical to a stall for its whole duration. When
  nothing advanced between intervals the line says so. Snapshots are
  appended to the tier's `status_events`.
- **`scripts/harness_notebook.py`** -- the interface notebooks use
  instead of hand-rolling HTTP. `OpencodeSession` (through the server,
  so tool use and routing behave as in an eval) and `OllamaModel`
  (residency, discovery, `/api/show` detail, and direct `/api/chat`
  including images and thinking control). Wraps `run_eval_client.py`'s
  own logic rather than restating it, so a notebook inherits its fixes.
  Model discovery reports both sources and marks Ollama-only entries;
  selection raises on an ambiguous match rather than picking; the
  Jupyter picker sets a choice while `resolve()` decides precedence, so
  a papermill parameter beats the widget and a headless run is not at
  the mercy of a dropdown default. `ipywidgets` added to the jupyter
  image for it, with graceful degradation when absent.
- **`scripts/vision_attachment_probe.py`** -- sends the same generated
  PNG down both the opencode and native-Ollama paths, so a delivery
  failure is distinguishable from an encoding rejection or a model that
  cannot see. See Known Limitations for what it found.
- **Terraform fails the plan when a container already holds a managed
  name.** Terraform plans against its own state, so a container Compose
  created is invisible: the operator approved a clean-looking plan and
  the apply then died at create time, after building the volume, the
  network and three images. `data.external.container_conflicts` plus a
  precondition on `docker_container.server` surfaces it during plan
  instead, at the existing approval point. Terraform-created containers
  now carry `managed-by` and `project` labels, so the check can report
  whether it found a Compose stack, a Terraform leftover whose state was
  lost, or something unrelated, and tailor the message. Adoption via
  import is deliberately not offered.

### Changed

- The e2e hang is no longer attributed to a blocked outbound call.
  Earlier entries and a `REQUIREMENTS.md` dependency row both said the
  mock backend's empty request log pointed at egress from opencode's own
  startup path. Measurement disproved it: the cause is the readiness
  race above, reproduced on 1.18.3 and 1.18.23 with no network
  involvement. The dependency row is gone — `REQUIREMENTS.md` states what
  is required, not what was once believed — and the failure message in
  `test_run_eval_client_e2e.py` no longer sends a reader after a network
  problem that does not exist.

- `scripts/test_run_eval_client_e2e.py` pins opencode 1.18.23, matching
  the image. The flat-JSON test asserts the harness detects a
  non-conforming backend; opencode 1.18.21 added `"unknown"` to the
  loop-exit list in `session/prompt.ts`, and the previous assertion
  pinned the pre-fix behaviour.
- The development host name is removed from tracked files. `make verify`
  fails on it rather than counting it, and excludes itself by pathspec.

### Fixed

- `test_run_eval_client_e2e.py` waits for an answered HTTP request
  rather than an open port. `opencode serve` accepts connections ~1.5s
  before its route layer serves; a request in that window is received,
  drained from the socket buffer, and never answered, so no finite
  timeout catches it. Post at 0s blocks past 40s; post at 5s returns 200
  in ~120ms. Reproduces on 1.18.3 and 1.18.23.
- `run_eval_client.py` aborts a tier when the provider cannot end a
  turn. A backend answering `stream:true` with non-streaming JSON
  reports no finish reason, so opencode keeps prompting at ~15 calls/s
  until the tier times out. `_unproductive_loop()` is a separate
  predicate from `_progress_is_moving()`, which cannot detect this:
  `messages` climbs, so the session reads as healthy.
- `run_eval_client.py` captures the whole `/session/{id}/message` chain
  and child sessions, and builds the transcript from it. Previously only
  the final response object was recorded, so subagent tool use did not
  reach the scanner; the scored turn's tools were also read from the
  response object, where they do not appear.
- `tools/pipeline.sh`: `stage_e2e` resolves the host through
  `scripts/hostnet.py`. `localhost` is the worker, not the host, under a
  CI runner. Its embedded Python is also single-line — indentation
  damage previously raised `IndentationError` into `/dev/null`, so the
  stage could not pass in any environment.
- `tools/pipeline.sh` lints itself, and reports directory artifacts as
  file count and size rather than a malformed byte count.
- `scripts/tools/mock_openai_backend.py` emits the usage chunk
  `stream_options` requests. Without it `Session.getUsage()` returns an
  empty `Usage` and runs record zero tokens. The e2e test asserts
  non-zero input and output.
- `scripts/tools/axiom_cvv_verify.py` usage block names its own path.
- Two test files closed the sockets they opened. `HTTPServer.shutdown()`
  ends the serve loop and does not close the listening socket;
  `server_close()` is a separate call. Both leaked on every run.
- `docs/BRANCHING.md`'s history-rewrite check dereferences annotated
  tags. `%(objectname)` returns the tag object rather than the commit, so
  the check as written found branches and missed every release tag.


- **The end-to-end suite no longer leaks its `opencode serve`
  process.** Cleanup called `kill()` without a wait, so the child was
  never reaped and Python reported it still running at interpreter
  exit. Cleanup now kills, waits, and drains the pipe. This removes the
  leak; it did not resolve the suite's intermittent hang at session
  creation, which remains open -- the readiness check is a TCP connect,
  which succeeds as soon as the listener binds and before the server
  can answer, and that is the current lead rather than a confirmed
  cause.

- **A tier whose scoring tool could not run recorded a PASS.**
  `scan_transcript()` had three failure paths -- non-zero exit,
  unparseable stdout, empty result list -- all returning the same empty
  counts as a clean scan, and an empty count set satisfies
  `must_not_have_categories` trivially. So a CVV-only tier passed while
  no CVV scan happened, with the reason "pass_criteria satisfied".
  Observed live. The scanner now reports whether it ran and why not,
  and `check_pass()` refuses first: a check that did not execute is not
  a pass. A result dict without the flag also fails, so any path not
  updated errs toward refusing.
- **The transcript could not contain the evidence it was scanned for.**
  Tool calls live in the session's message chain, not the final
  response, so a tier whose work spanned webfetch, grep and subagent
  dispatch produced a transcript with none of those markers -- CVV
  categories judging a claim made without a verification attempt were
  matched against text that structurally could not show one.
  `session_tool_calls()` walks the chain and follows child sessions,
  since the `task` tool puts its work in one. The calls also land in
  `tierN.raw.json`. The calls are attributed to the setup turn as a
  group; the chain does not cleanly partition by which prompt triggered
  which call, and inventing an attribution would be worse.
- **Elapsed time counted host suspend as compute.** Wall-clock keeps
  running while a suspended machine is frozen, so a tier that worked
  two minutes and then slept four hours reported 16835s -- and that
  reading was taken as a runaway agentic loop when the host had
  been asleep. `time.monotonic()` does not advance across suspend, so
  the divergence between the two since the last sample measures the
  sleep. The heartbeat now names it, records `suspended_s`, and does
  not report the frozen counters as a stall.
- **`cvv_scan.py` could not fire on a hedge-drop.**
  `BLANKET_CLOSING_ASSESSMENT` gates on a hedge appearing earlier in
  the turn, but `HEDGE_WORDS` held eight phrases and contained none of
  `might`, `not certain`, `unsure`, `possibly`, `seems`. Measured false
  negative. Extended -- and the limit written into the file: every
  detector here matches fixed phrases against prose, so every failure
  is a recall failure and the enumeration cannot be completed. A patch,
  not a solution.
- **`TOOLS_DIR` was fixed at `/opt/harness/tools`**, so a container
  started with `--entrypoint` could not find `cvv_scan.py`, logged a
  warning, and recorded a PASS on an empty findings set -- a CVV-only
  tier passing while the CVV scan never ran. `TASK_SUITE_DIR`,
  `RESULTS_DIR` and `TOOLS_DIR` now fall back to the checkout and are
  overridable. The path resolution is fixed; the underlying scoring
  vacuity is not -- see Known Limitations.

## [1.0.0] - 2026-07-27

### Changed

- **Breaking:** removed this project's own static `provider["local/ollama"]["models"]`
  list from `config/opencode.base.json` and `config/opencode.git-workspace.json`.**
  Every container that runs opencode now mounts your
  `~/.config/opencode/opencode.json` read-only and merges it *under* the
  project's own config (confirmed via source: `config.ts`'s `loadGlobal()`
  always loads first, `OPENCODE_CONFIG` overlays on top -- an override,
  not a replacement, so the project's `permission`/`baseURL` settings still
  win regardless of what your global file sets on this same provider).
  **Requires exporting `OPENCODE_GLOBAL_CONFIG`** (absolute path, not
  defaulted to a `~`-prefixed path -- confirmed via `docker/compose`
  issues #6506/#3872 that tilde expansion in a Compose volume path is
  inconsistent) before running `docker-compose` or `terraform apply`, and
  requires your own global config to declare
  `provider["local/ollama"]["models"]` for whatever local models you use.
  See REQUIREMENTS.md and INSTALL.md for the exact format expected.
  This replaces having to hand-edit 2-3 JSON files every time a new local
  model is pulled with editing exactly one -- your own global config --
  which you'd naturally already be keeping current for other opencode use.

## [0.2.0] - 2026-07-25

### Added

- **Server-side, provider-scoped session TTL** (`scripts/session_reaper.py`).
  opencode has no native session TTL/idle-expiry -- confirmed by source
  search across `packages/opencode/src/session/`. Runs as a background
  loop inside the `server` container, polling `GET /session` and calling
  `DELETE /session/{id}` on anything idle past its TTL (confirmed via
  source that this cancels in-flight work first, rather than a bookkeeping
  delete). `local/ollama` sessions get an aggressive 10min TTL --
  sustained Ollama residency is the resource cost this cuts.
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
- Committed e2e test behind `run_eval_client.py`'s response-schema
  claim (`scripts/test_run_eval_client_e2e.py` +
  `scripts/tools/mock_openai_backend.py`) -- installs
  `opencode-ai` via npm, runs `opencode serve` as a subprocess, drives
  it through this repo's client functions against a
  SSE-emitting mock backend. Not yet observed passing end-to-end in
  every environment tried -- see Known Limitations.
- Quota/rate-limit exhaustion detected and reported distinctly (`Q`
  tier mark) instead of blocking indefinitely or risking a duplicate
  message. `quota_aware_send_message()` polls `GET /session/status`
  concurrently with the request and aborts cleanly past
  `OPENCODE_QUOTA_WAIT_THRESHOLD_S` (default 3000s/50min) rather than
  re-POSTing to a session that might still be processing.
- Per-run server-side log capture (`server.log` alongside
  `report.json`), filtered by this run's own session IDs and error
  refs -- a raw whole-file copy would mix in every other run's
  interleaved lines on a persistent shared server.
- Live per-round-trip progress dots during a run, and per-tier HTTP
  errors (`E` mark) no longer crash the whole run.
- `git-workspace` role (`bash: allow`/`edit: allow`, isolated by
  mounting nothing but read-only `auth.json`) as an isolated place to run
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
  Removed a `for_each` Terraform resource that was launching 5
  eval-client runs against Ollama in parallel on every plain "Deploy
  harness", regardless of `must_run = false` -- confirmed via a recorded
  deploy log. Local models now run through the exact same one-shot
  mechanism cloud models use.
- **Terraform: `ollama_base_url` split into `opencode_ollama_base_url`
  (keeps `/v1`) and `ollama_native_base_url` (no `/v1`).** One shared
  variable was feeding two consumers with incompatible URL
  shapes -- confirmed from Ollama's own source (`server/routes.go`):
  the OpenAI-compat surface (`/v1/*`) and the native API (`/api/*`)
  are two disjoint, hardcoded route trees; neither is derived from the
  other.
- **Dockerfile split into `server` (light) and `harness` (heavy,
  extends `server`) stages.** Every prior build failure traced back to
  dependencies the `server` role never used (spaCy,
  onnxruntime, click, PEP 668, a BuildKit `--chmod` requirement
  the development host's Docker doesn't support). `server` now only installs
  `ca-certificates` + `python3`.
- **`auth-data/auth.json` extraction now automatic under Terraform**
  (`data.external.auth_keys`, wrapping `scripts/extract-opencode-key.sh
  --all`) -- fixes a failure where Docker silently creates an
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
  independent paths -- root cause was the development host's Docker using the
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
  is, at any depth.
- Ollama cold-start: a fresh local/ollama run's first tier was timing
  out at exactly 300s with the server busy, not failing --
  matches Ollama's documented 5-minute idle-unload eating into the
  tier's own budget. Fixed with an explicit warm-up call before the
  test ladder (`OPENCODE_WARMUP_TIMEOUT_S`, 600s default), plus
  explicit unload after the run finishes.

### Known Limitations

- **A purely negative pass criterion cannot distinguish a refutation
  from silence.** The vacuous-pass-on-unrun-scan case is fixed above,
  but a tier carrying only `must_not_have_categories` is still
  satisfied by an empty reply exactly as by a good answer.
  Tiers need something positive to pass on, which is a change to
  `test_ladder.json` rather than to the client.
- **`cvv_scan.py` detection remains recall-limited by construction.**
  The hedge lexicon gap is fixed, but every detector matches fixed
  phrases against prose, so a violation phrased in unlisted words is
  invisible and a miss becomes a PASS. Four bugs of this family so far.
  Closing the class needs a second, non-regex pass -- cheap regex
  first, a model consulted only where regex found nothing, with the
  verdict still decided by code.
- **Image attachments do not reach the model on `local/ollama`.** The
  v1 API accepts a `FilePartInput` and the server takes it, but the
  model reports no image support. Same bytes direct to Ollama's
  `/api/chat` are described correctly. Upstream
  anomalyco/opencode#20802. Also: opencode passes only text and image
  media to the model, silently excluding PDF/AVIF/BMP/audio/video, and
  Ollama's `/v1` accepts only base64 data URLs for
  jpeg/jpg/png/webp.
- **Elapsed times are wall-clock, and the suspend case is now
  reported.** A machine that sleeps mid-run still has the sleep counted
  in a tier's elapsed seconds, and every wall-clock timeout (the 300s
  message bound, `OPENCODE_WARMUP_TIMEOUT_S`, the quota threshold) can
  still fire on resume for a request that was never slow. The
  heartbeat now detects and names the suspend rather than leaving the
  inflated figure to be misread, but the timeouts themselves are
  unchanged.
- **`scripts/test_run_eval_client_e2e.py` has not been observed
  passing cleanly in every environment tried.** In a network-restricted
  sandbox, `POST /session` hung indefinitely with the mock backend's
  request log staying completely empty -- points at an outbound call
  opencode itself makes during session creation, to a domain outside
  a restricted allowlist. On a machine with full network access
  (the development host), one of the two tests in the suite still failed the same
  way (`create_session` timing out at the same 20s bound) while the
  other passed -- suggesting something timing-sensitive rather than a
  hard categorical block. Root cause not yet confirmed; a live
  `strace`/`tcpdump` capture during the hang is the next
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
  working end-to-end against a live Ollama instance from every
  environment this project has been developed in.
