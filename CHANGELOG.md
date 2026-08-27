# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Tagged versions are created after merging to `main`, per
`docs/BRANCHING.md`'s delivery convention -- see that file's
"Versioning" section for the MAJOR/MINOR/PATCH rules applied here.

## [Unreleased]

### Fixed

- **The e2e test had never passed since it was written.** Five weeks of
  "intermittency" was a test born broken, against two documented but
  never-tested hypotheses. Measured cause: `opencode serve` accepts TCP
  connections roughly 1.5s before its route layer can serve, and a
  request landing in that window is accepted, drained out of the kernel
  receive buffer, and never answered at all -- so no finite timeout can
  catch it, which is why widening one never helped. Post 0s after the
  port opens: blocked past 40s, every time; post 5s: HTTP 200 in ~120ms.
  Reproduced on 1.18.3 and 1.18.23, so it is not version-specific. The
  test now requires an answered HTTP request before its timed section
  begins. `scripts/trace_session_hang.py` is the harness that settled
  it, and records the result in its own docstring so the matrix is not
  re-run blind.
- **A non-conforming provider was scored as a model failure.** A backend
  that answers a `stream:true` request with non-streaming JSON never
  reports a finish reason, and opencode consequently cannot end the
  turn -- one assistant message per provider call, measured at ~15/s.
  Left alone that ran to a 300s timeout and scored as if the model had
  failed, while the provider was charged for every call.
  `_unproductive_loop()` detects it and aborts the tier as a
  provider-conformance fault. It needs a second predicate because the
  existing stall check cannot see it: `messages` climbs by hundreds, so
  the session reads as healthy.
- **The transcript could not contain the evidence it was scanned for.**
  Only the final response object was captured, so a tier whose server
  log showed 21 steps of tool use produced a transcript with none. The
  whole `/session/{id}/message` chain plus child sessions is now
  recorded, and the transcript is rebuilt from it -- which also fixes
  the misattribution in the old assembly, where a setup turn with any
  tool call of its own discarded the session-wide capture and the scored
  turn was handed tool calls from the response object, precisely where
  they do not appear.
- **`stage_e2e` could not pass in any environment.** Both of its
  embedded Python blocks had acquired a leading space on every line, and
  the reachability probe sent the resulting `IndentationError` to
  `/dev/null`, so the stage reported SKIPPED even on a host with Ollama
  on `localhost`. Also fixed: `localhost` is the worker rather than the
  host when this runs under a CI runner -- `scripts/hostnet.py` now owns
  the candidate chain in one place.
- **Every e2e run recorded zero tokens.** The mock backend ignored the
  `stream_options` opencode sends on every request, so no usage chunk
  was emitted and `Session.getUsage()` fell back to an empty `Usage` --
  which looked exactly like a correct result. The test now asserts
  non-zero input and output tokens.
- **Lint did not check its own driver.** `tools/pipeline.sh` was absent
  from `shell_files()`, which is how a syntax error survived there.

### Added

- **Provider-request capture** (`scripts/tools/capture_proxy.py`,
  opt-in). opencode assembles the system array and tool definitions per
  request and never persists them, so the outbound provider request is
  the only place the resolved instruction set exists as bytes -- the
  difference between recording what a model answered and what it was
  asked. Enabled by setting `OPENCODE_CAPTURE_PROXY`; nothing sits in
  the inference path otherwise. Parses the request and relays the
  response byte for byte, so chunked framing and SSE delivery are
  unaltered. A plain-HTTP provider is fully visible; an HTTPS provider
  arrives as CONNECT with an opaque body, and each record says so.
- **Catalog warm-up before `serve`.** `opencode models --refresh` runs
  in the entrypoint, gated and non-fatal, moving 191 npm connections out
  of the first request's path.

### Changed

- **The development host's name is scrubbed from tracked files** -- 20
  occurrences across 8 files, all comments and variable descriptions.
  The `verify` check that reported them as a standing note is now a hard
  failure, since the baseline is zero and any hit is a new
  reintroduction into a public repo. It excludes itself by pathspec:
  the pattern is the hostname, so the file matched itself and the check
  could never reach zero.
- **The e2e test's opencode pin moved 1.18.3 -> 1.18.23**, matching the
  build the image ships. The old pin verified a response schema for a
  build nobody ran. Note for anyone bisecting: the flat-JSON behaviour
  change at 1.18.21 is a deliberate fix, not a regression -- an
  `unknown` finish no longer ends a turn silently.

### Added

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
  real work and a client wedged on a dead socket produced identical
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
  so tool use and routing behave as in a real eval) and `OllamaModel`
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

### Fixed

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
  reading was taken as a runaway agentic loop when the host had simply
  been asleep. `time.monotonic()` does not advance across suspend, so
  the divergence between the two since the last sample measures the
  sleep. The heartbeat now names it, records `suspended_s`, and does
  not report the frozen counters as a stall.
- **`cvv_scan.py` could not fire on a genuine hedge-drop.**
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

### Changed (BREAKING)

- **Removed this project's own static `provider["local/ollama"]["models"]`
  list from `config/opencode.base.json` and `config/opencode.git-workspace.json`.**
  Every container that runs opencode now mounts your real
  `~/.config/opencode/opencode.json` read-only and merges it *under* the
  project's own config (confirmed via source: `config.ts`'s `loadGlobal()`
  always loads first, `OPENCODE_CONFIG` overlays on top -- an override,
  not a replacement, so the project's `permission`/`baseURL` settings still
  win regardless of what your global file sets on this same provider).
  **Requires exporting `OPENCODE_GLOBAL_CONFIG`** (absolute path, not
  defaulted to a `~`-prefixed path -- confirmed via real `docker/compose`
  issues #6506/#3872 that tilde expansion in a Compose volume path is
  inconsistent) before running `docker-compose` or `terraform apply`, and
  requires your own global config to actually declare
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
  the development host's Docker doesn't support). `server` now only installs
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
  actually is, at any depth.
- Ollama cold-start: a fresh local/ollama run's first tier was timing
  out at exactly 300s with the server genuinely busy, not failing --
  matches Ollama's documented 5-minute idle-unload eating into the
  tier's own budget. Fixed with an explicit warm-up call before the
  real test ladder (`OPENCODE_WARMUP_TIMEOUT_S`, 600s default), plus
  explicit unload after the run finishes.

### Known Limitations

- **A purely negative pass criterion cannot distinguish a refutation
  from silence.** The vacuous-pass-on-unrun-scan case is fixed above,
  but a tier carrying only `must_not_have_categories` is still
  satisfied by an empty-but-genuine reply exactly as by a good answer.
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
