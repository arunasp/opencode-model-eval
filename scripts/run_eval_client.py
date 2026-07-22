#!/usr/bin/env python3
"""Runs the structured, escalating-difficulty test ladder
(task-suite/test_ladder.json) against a running `opencode serve`
instance over HTTP, and scores each tier with cvv_scan.py.

Uses stdlib `urllib` only, no new dependency for the HTTP layer itself
(matches this repo's CODEGEN.md preference for stdlib over new deps).

REQUEST SCHEMA -- confirmed from opencode's actual source, not guessed:
  POST {base_url}/session
    body: {} or partial Session.CreateInput (all fields optional:
          parentID, title, agent, model, metadata, permission,
          workspaceID) -- (session/session.ts:260-271)
    -> Session.Info (used for its "id" field)

  POST {base_url}/session/{sessionID}/message
    body per PromptInput minus sessionID (session/prompt.ts:1499-1520):
      {
        "model": {"providerID": "...", "modelID": "..."},   (ModelRef,
                   session/prompt.ts:1494-1497)
        "parts": [{"type": "text", "text": "..."}]           (discriminated
                   union on "type", session/prompt.ts:1512-1519)
      }
    -> SessionV1.WithParts

RESPONSE SCHEMA -- CONFIRMED empirically, not just from source. Ran a
real opencode serve instance (opencode-ai@1.18.3 via npm) against a
mock OpenAI-compatible backend under my own control and captured the
actual response. THIS IS NOW A COMMITTED, RE-RUNNABLE TEST, not just a
prior session's claim left undocumented in the repo --
see scripts/test_run_eval_client_e2e.py and
scripts/tools/mock_openai_backend.py:
    {
      "info": {..., "finish": "stop", "id": "msg_...", "sessionID": "..."},
      "parts": [
        {"type": "step-start", ...},
        {"type": "text", "text": "the actual reply", ...},
        {"type": "step-finish", "reason": "stop", ...}
      ]
    }
extract_reply()'s primary path (top-level "parts", filter on
type == "text") matches this exactly. One real thing the first empirical
attempt got wrong before this was confirmed: opencode's request to the
backend sets "stream": true, and a mock that responds with a flat
synchronous JSON body (rather than real SSE chunks) produces a
step-start/step-finish pair with NO text part at all -- silently wrong,
not an error. Fixed by having the mock emit actual
`data: {...}\n\n` SSE chunks. Also observed empirically, worth knowing
for request-count/cost expectations: opencode fires an extra
background title-generation call (a short system-prompted request
asking for a thread title) before the real one, per session.

Tool-call part shape specifically was NOT exercised in this empirical
test (the mock never triggered a tool call) -- extract_reply()'s
`"tool" in ptype.lower()` branch is a reasonable inference consistent
with the now-confirmed type-discriminated-parts-array pattern, but that
specific branch remains unverified against real tool-call output.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import http.client
import urllib.error
import urllib.request
from pathlib import Path

TASK_SUITE_DIR = Path("/task-suite")
RESULTS_DIR = Path("/results")
TOOLS_DIR = Path("/opt/harness/tools")
# Shared, read-only mount (docker-compose.yml/terraform's opencode_log
# volume) -- opencode's OWN log file, the same one previously only
# reachable via `docker exec ... cat`. Confirmed path from actual
# container inspection this session: opencode.log specifically, not a
# directory of rotated files (at least as of the version tested).
OPENCODE_LOG_PATH = Path("/home/harness/.local/share/opencode/log/opencode.log")

# Quota/rate-limit awareness config -- all tunable via env var, no
# hardcoded provider-specific knowledge (NVIDIA vs Zen vs anything
# else). opencode's own retry.ts already translates provider-specific
# behavior (real Retry-After headers, provider error-body patterns)
# into a single "next attempt at this timestamp" signal via
# GET /session/status -- this harness only needs ONE threshold applied
# uniformly to that signal, not per-provider branching.
#
# NOT the same thing as backlog item 1 ("eval tests keep crunching past
# Claude's own usage-quota ceiling until a separate 5-minute timeout
# elsewhere stops them"). This mechanism is about the MODEL PROVIDER's
# quota/rate limit (OpenCode Zen/Go, NVIDIA, etc.), observed via
# opencode's own /session/status -- it already works correctly and is
# unrelated to whatever session/tool-execution limit governs the
# Claude Code agent driving this harness itself. That's out of scope
# for this repo: there's no hook here into Claude Code's own runtime,
# and no evidence that anything at this layer is the cause. Don't
# conflate the two if this surfaces again.
QUOTA_WAIT_THRESHOLD_S = float(os.environ.get("OPENCODE_QUOTA_WAIT_THRESHOLD_S", "3000"))  # 50 min default
STATUS_POLL_INTERVAL_S = float(os.environ.get("OPENCODE_STATUS_POLL_INTERVAL_S", "5"))
# Heartbeat cadence for stdout progress during a long-running tier --
# deliberately separate from STATUS_POLL_INTERVAL_S (which stays fast,
# 5s, for responsive quota-threshold detection). Printing on every 5s
# poll would flood results/logs/ over a 50-minute wait; this caps
# actual stdout output to once a minute by default while polling
# itself stays frequent underneath.
PROGRESS_PRINT_INTERVAL_S = float(os.environ.get("OPENCODE_PROGRESS_PRINT_INTERVAL_S", "60"))


class _QuotaExhausted(Exception):
    """Internal signal, not a real error -- raised inside run_category()
    to unify the "gave up waiting on a quota/rate-limit stall" path
    with the existing try/except structure, rather than threading an
    extra return-value check through both call sites. Never escapes
    run_category() itself.
    """
    def __init__(self, quota_info: dict, events: list[dict]):
        self.quota_info = quota_info
        self.events = events
        super().__init__(quota_info.get("message", "quota exhausted"))


def http_post(base_url: str, path: str, body: dict, timeout: int = 300) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} failed: HTTP {e.code}: {body_text[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"POST {path} failed to reach {url}: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Reached the server, got a 200, but the body wasn't valid
        # JSON/UTF-8 -- e.g. an HTML error page from a proxy in front
        # of opencode, a truncated-but-Content-Length-satisfied body,
        # or a non-JSON success response from some future opencode
        # version. Previously completely unhandled: json.loads()'s
        # exception isn't urllib.error.*, isn't OSError, isn't
        # TimeoutError -- it's a ValueError subclass, disjoint from
        # every network-layer exception this function already caught,
        # so it would have crashed the run exactly like the others did
        # before being fixed.
        raise RuntimeError(f"POST {path} returned a response that wasn't valid JSON/UTF-8: {e}") from e
    except TimeoutError as e:
        # Hit live: the connection succeeds (request sent fine) but the
        # server never finishes sending a response within `timeout`
        # seconds -- raised deep inside http.client.getresponse(), NOT
        # a urllib.error.URLError subclass, so it previously sailed
        # straight past both except clauses above as a raw traceback
        # that crashed the entire eval run -- confirmed the run_category
        # try/except RuntimeError (added alongside this file's progress
        # dots) could not have caught this either, since http_post never
        # translated it to RuntimeError in the first place. Must come
        # before the `except OSError` below -- TimeoutError IS an
        # OSError subclass, so ordering matters (first matching except
        # wins).
        raise RuntimeError(f"POST {path} timed out after {timeout}s waiting for a response from {url}") from e
    except http.client.HTTPException as e:
        # Protocol-level failures below urllib's own error hierarchy:
        # IncompleteRead (connection closed before Content-Length bytes
        # were fully read), BadStatusLine, LineTooLong, etc. Confirmed
        # against Python's own docs before assuming coverage: these are
        # HTTPException subclasses, NOT OSError subclasses (the one
        # exception, RemoteDisconnected, inherits both -- caught here
        # first since this branch comes before the OSError catch-all,
        # giving it the more specific message). Would otherwise have
        # slipped past the OSError catch-all below exactly the way
        # TimeoutError slipped past URLError.
        raise RuntimeError(f"POST {path} failed: HTTP protocol error: {e}") from e
    except OSError as e:
        # Catch-all for other socket-level failures that also don't
        # route through urllib.error (connection reset, broken pipe,
        # etc.) -- same reasoning as TimeoutError above: every
        # network-layer failure becomes a RuntimeError, which callers
        # (run_category's per-tier catch) already know how to handle.
        raise RuntimeError(f"POST {path} failed: network error: {e}") from e


def create_session(base_url: str) -> str:
    resp = http_post(base_url, "/session", {})
    session_id = resp.get("id") or resp.get("sessionID")
    if not session_id:
        raise RuntimeError(f"session creation response had no id/sessionID field: {resp}")
    return session_id


def http_get(base_url: str, path: str, timeout: int = 10) -> dict:
    """Mirrors http_post's full exception-to-RuntimeError translation --
    same reasoning applies identically to GET requests (status/message
    fetches), just without a request body. Kept as a near-duplicate of
    http_post rather than a shared helper with a method= parameter:
    the two already diverge slightly (POST needs a body/Content-Type,
    GET's error branches don't need the HTTPError body-reading dance
    the same way) and forcing them into one function would trade a
    small amount of duplication for a worse abstraction.
    """
    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {path} failed: HTTP {e.code}: {body_text[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GET {path} failed to reach {url}: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(f"GET {path} returned a response that wasn't valid JSON/UTF-8: {e}") from e
    except TimeoutError as e:
        raise RuntimeError(f"GET {path} timed out after {timeout}s waiting for a response from {url}") from e
    except http.client.HTTPException as e:
        raise RuntimeError(f"GET {path} failed: HTTP protocol error: {e}") from e
    except OSError as e:
        raise RuntimeError(f"GET {path} failed: network error: {e}") from e


def get_session_status(base_url: str, session_id: str) -> dict:
    """GET /session/status returns a map of ALL sessions' statuses, not
    just ours (confirmed from source: session/status.ts's underlying
    store is keyed by sessionID across the whole server) -- this reads
    just our session's entry out of that map.

    Defaults to {"type": "idle"} if our session isn't in the map at
    all, matching opencode's own default exactly (source:
    `data.get(sessionID) ?? { type: "idle" as const }`) -- a session
    that's never been busy/retrying (or one the server has no record
    of, e.g. right after creation before any message) is legitimately
    idle, not an error condition.

    Real status.type values, confirmed from source (session/status.ts,
    session/run-state.ts, session/processor.ts) -- NOT guessed, no
    other values exist in the codebase: "idle", "busy", "retry". The
    "retry" case additionally carries attempt/message/action/next
    (all from session/retry.ts's SessionRetry.policy -- see
    quota_aware_send_message()'s docstring for what these mean).
    """
    statuses = http_get(base_url, "/session/status")
    return statuses.get(session_id, {"type": "idle"})


def abort_session(base_url: str, session_id: str) -> bool:
    """POST /session/{id}/abort -- confirmed from source
    (server/routes/.../groups/session.ts): "Abort an active session
    and stop any ongoing AI processing or command execution." This is
    the safe way to give up on a stuck/quota-exhausted attempt --
    NEVER just re-POST a new message to the same session while the
    original might still be processing server-side (opencode's own
    retry loop, confirmed unbounded by attempt count or wall-clock
    time in session/retry.ts, is decoupled from whether our client
    connection is even still attached -- a second POST would risk a
    genuine duplicate turn in the transcript, not just a wasted retry).
    """
    result = http_post(base_url, f"/session/{session_id}/abort", {})
    return bool(result)


def send_message(base_url: str, session_id: str, provider: str, model_id: str, text: str) -> dict:
    body = {
        "model": {"providerID": provider, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    return http_post(base_url, f"/session/{session_id}/message", body)


def quota_aware_send_message(base_url: str, session_id: str, provider: str, model_id: str, text: str,
                              quota_wait_threshold_s: float = QUOTA_WAIT_THRESHOLD_S,
                              poll_interval_s: float = STATUS_POLL_INTERVAL_S) -> tuple[dict | None, dict | None, list[dict]]:
    """Wraps send_message() with concurrent, non-blocking status
    awareness -- NOT a retry mechanism itself, since opencode already
    has one (session/retry.ts, confirmed unbounded by attempt count or
    wall-clock time, confirmed no config-level cap exists anywhere in
    the codebase). This exists to detect when that internal retry has
    stalled beyond a reasonable wait (a real quota ceiling: OpenCode
    Zen/Go daily limit or balance exceeded, a provider rate-limit
    cooldown, etc.) and give up CLEANLY -- abort first, never a second
    blind POST to the same session, which could duplicate a turn if
    the original attempt is still processing server-side.

    Mechanism: send_message() runs in a background thread (its own
    http_post call blocks exactly as before, unchanged). The MAIN
    thread concurrently polls GET /session/status every
    poll_interval_s -- this is a cheap, separate HTTP call, not
    related to the blocking POST at all. If status.type == "retry"
    and its "next" field (an absolute ms-epoch timestamp for
    opencode's own next attempt -- confirmed from session/retry.ts:
    `next: now + wait`) implies a wait longer than
    quota_wait_threshold_s, this calls abort_session() and returns a
    quota-exhaustion signal instead of continuing to wait.

    Returns (result, quota_info, events):
      - Normal completion: (SessionV1.WithParts dict, None, events)
      - Quota-exhaustion bailout: (None, {"reason": ..., "wait_seconds":
        ..., "message": ...}, events)
      - A real error (network/HTTP/etc.) from send_message() itself
        propagates as a raised RuntimeError, same as calling
        send_message() directly -- this function only adds NEW
        behavior for the quota case, it doesn't change error handling
        for anything that already worked.
    events is every distinct status snapshot observed while polling
    (timestamp + full status dict) -- the per-tier "what actually
    happened" log, not just the terminal outcome. A tier that passed
    but needed several internal opencode retries against a rate limit
    looks different in this list than one that passed cleanly on the
    first attempt, even though both end up PASS in the tier record.
    """
    result_holder: dict = {}
    exception_holder: dict = {}
    done_event = threading.Event()
    events: list[dict] = []

    def worker():
        try:
            result_holder["value"] = send_message(base_url, session_id, provider, model_id, text)
        except Exception as e:  # noqa: BLE001 -- deliberately broad, re-raised verbatim below
            exception_holder["value"] = e
        finally:
            done_event.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    wait_start = time.time()
    last_progress_print = wait_start
    last_status_type = None
    while True:
        if done_event.wait(timeout=poll_interval_s):
            break  # worker finished (success or exception) during this poll window

        try:
            status = get_session_status(base_url, session_id)
        except RuntimeError:
            # Status check itself failed transiently (e.g. a momentary
            # network blip on OUR polling connection, unrelated to the
            # actual message request) -- don't act on missing
            # information, just try again next interval. The worker
            # thread's own connection is entirely separate and
            # unaffected by this.
            continue

        status_type = status.get("type")
        now = time.time()
        if status_type != last_status_type:
            events.append({"timestamp": now, **status})
            print(f"[eval-client] status changed: {last_status_type} -> {status_type} "
                  f"(elapsed {now - wait_start:.0f}s)", flush=True)
            last_status_type = status_type
            last_progress_print = now
        elif status_type == "retry" and now - last_progress_print >= PROGRESS_PRINT_INTERVAL_S:
            # Heartbeat while stuck in the same state -- without this,
            # a single stalled/retrying tier produces zero stdout for
            # up to QUOTA_WAIT_THRESHOLD_S (50min default), which is
            # exactly the "no way to tail a slow run's live progress"
            # gap this fixes.
            events.append({"timestamp": now, **status})
            print(f"[eval-client] still waiting on retry (elapsed {now - wait_start:.0f}s, "
                  f"threshold {quota_wait_threshold_s:.0f}s)", flush=True)
            last_progress_print = now

        if status_type == "retry":
            next_ms = status.get("next")
            if next_ms is not None:
                wait_s = (next_ms / 1000.0) - time.time()
                if wait_s > quota_wait_threshold_s:
                    try:
                        abort_session(base_url, session_id)
                    except RuntimeError:
                        pass  # best-effort -- we're bailing on this tier regardless
                    # Give the worker thread a brief window to unwind
                    # after the abort (its blocking POST should now
                    # return, likely with an error) rather than leaving
                    # it dangling as an orphaned daemon thread forever.
                    done_event.wait(timeout=poll_interval_s)
                    action = status.get("action") or {}
                    return None, {
                        "reason": action.get("reason", "unknown"),
                        "wait_seconds": wait_s,
                        "message": status.get("message", ""),
                    }, events

    if "value" in exception_holder:
        raise exception_holder["value"]
    return result_holder.get("value"), None, events


def extract_reply(response: dict) -> tuple[str, list[dict]]:
    """Extracts (assistant_text, tool_calls) from a SessionV1.WithParts
    response. Primary path (top-level "parts", filter on type=="text")
    confirmed empirically against a real opencode serve instance -- see
    module docstring. The message.parts fallback and the tool-call
    branch remain defensive/inferred, not exercised by that same test.
    """
    parts = response.get("parts")
    if parts is None and isinstance(response.get("message"), dict):
        parts = response["message"].get("parts")
    if parts is None:
        parts = []

    text_chunks = []
    tool_calls = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype == "text":
            text_chunks.append(part.get("text", ""))
        elif "tool" in ptype.lower():
            tool_calls.append(part)
    return "\n".join(text_chunks), tool_calls


def events_to_transcript(setup_message: str, setup_text: str, setup_tools: list[dict],
                          probe_message: str, probe_text: str, probe_tools: list[dict]) -> str:
    lines = []

    def render_turn(user_msg: str, text: str, tools: list[dict]):
        lines.append("## User")
        lines.append(user_msg)
        lines.append("")
        lines.append("## Assistant")
        for t in tools:
            tool_name = t.get("tool") or t.get("name") or "unknown"
            lines.append(f"**Tool: {tool_name}**")
            if "input" in t:
                lines.append("**Input:**")
                lines.append("```json")
                lines.append(json.dumps(t["input"], indent=2))
                lines.append("```")
            if "output" in t or "result" in t:
                lines.append("**Output:**")
                lines.append("```")
                lines.append(str(t.get("output", t.get("result", "")))[:2000])
                lines.append("```")
        if text:
            lines.append(text)
        lines.append("")

    render_turn(setup_message, setup_text, setup_tools)
    render_turn(probe_message, probe_text, probe_tools)
    return "\n".join(lines)


def scan_transcript(transcript_path: Path) -> dict:
    import subprocess
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "cvv_scan.py"), "--json", str(transcript_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"[warn] cvv_scan.py failed on {transcript_path}: {result.stderr}", file=sys.stderr)
        return {"category_counts": {}, "total_findings": 0}
    try:
        parsed = json.loads(result.stdout)
        return parsed[0] if parsed else {"category_counts": {}, "total_findings": 0}
    except (json.JSONDecodeError, IndexError):
        return {"category_counts": {}, "total_findings": 0}


def check_pass(scan_result: dict, criteria: dict) -> tuple[bool, str]:
    counts = scan_result.get("category_counts", {})
    for forbidden in criteria.get("must_not_have_categories", []):
        if counts.get(forbidden, 0) > 0:
            return False, f"forbidden finding present: {forbidden} (x{counts[forbidden]})"
    for required in criteria.get("must_have_categories", []):
        if counts.get(required, 0) == 0:
            return False, f"required finding absent: {required}"
    if "manual_check" in criteria:
        return False, (
            "NEEDS_MANUAL_REVIEW: no CVV violation found, but this tier's "
            f"pass condition requires human/test confirmation: {criteria['manual_check']}"
        )
    return True, "pass_criteria satisfied (CVV-only tier)"


def extract_error_refs(text: str) -> set:
    """Pulls opencode error 'ref' identifiers (e.g. err_9f166d92) out of
    an HTTP error body string -- these appear in this file's own error
    messages (see http_post's HTTPError branch, which embeds the raw
    JSON body) and correlate to specific server-log lines that often
    carry NO session_id at all. Confirmed from a real captured log:
    the single most useful line for diagnosing a failure --
    'message=failed ref=err_... error="ProviderModelNotFoundError...'
    -- has no session.id/id=ses_ token whatsoever. Filtering a
    server-log excerpt purely by session_id would silently miss
    exactly the line most worth keeping; refs close that gap.
    """
    return set(re.findall(r'"ref":\s*"(err_[a-zA-Z0-9]+)"', text))


def filter_log_by_identifiers(log_text: str, identifiers: set) -> str:
    """Keeps only lines containing any of the given identifiers as a
    substring. Deliberately substring matching, not a specific
    key=value regex per identifier type -- session IDs/refs appear
    under different field names across different log line shapes
    (id=, session.id=, ref=), and a substring match catches all of
    them without needing to enumerate every field name opencode might
    use (including ones that might change in a future version).

    Empty identifiers falls back to the WHOLE log unfiltered, not an
    empty result -- an over-inclusive artifact is more useful than an
    accidentally-empty one if this run somehow produced no identifiers
    to filter by at all.
    """
    if not identifiers:
        return log_text
    return "\n".join(
        line for line in log_text.splitlines()
        if any(ident in line for ident in identifiers)
    )


def run_category(category: dict, base_url: str, provider: str, model_id: str,
                  setup_message: str, category_dir: Path) -> dict:
    cat_id = category["id"]
    print(f"[eval-client] category: {cat_id}: {category['description']}", file=sys.stderr)
    category_dir.mkdir(parents=True, exist_ok=True)
    ceiling = 0
    tier_results = []
    summary_dots = []  # one char per tier: . pass / F fail / R review / E error / Q quota-exhausted -- printed as a compact row at the end of this category, and again in main()'s final grid

    for tier_def in category["tiers"]:
        tier_num = tier_def["tier"]
        # Progress within a single tier, not just between tiers: a
        # single slow LLM response previously looked identical to a
        # hung process from the CLI's perspective (nothing printed
        # until the whole tier finished). One flushed dot per HTTP
        # round-trip instead -- visible movement during exactly the
        # kind of multi-minute single-request wait that prompted this.
        print(f"  [tier {tier_num}] source={tier_def['source']} ", end="", file=sys.stderr, flush=True)

        try:
            session_id = create_session(base_url)
            print(".", end="", file=sys.stderr, flush=True)
            setup_resp, quota_info, setup_events = quota_aware_send_message(
                base_url, session_id, provider, model_id, setup_message)
            if quota_info is not None:
                raise _QuotaExhausted(quota_info, setup_events)
            print(".", end="", file=sys.stderr, flush=True)
            setup_text, setup_tools = extract_reply(setup_resp)
            probe_resp, quota_info, probe_events = quota_aware_send_message(
                base_url, session_id, provider, model_id, tier_def["prompt"])
            if quota_info is not None:
                raise _QuotaExhausted(quota_info, setup_events + probe_events)
            print(".", end="", file=sys.stderr, flush=True)
            probe_text, probe_tools = extract_reply(probe_resp)
        except _QuotaExhausted as e:
            # Distinct from a generic RuntimeError below on purpose:
            # this means opencode's OWN retry loop (session/retry.ts,
            # confirmed unbounded) was still legitimately working when
            # we gave up waiting -- nothing is wrong with the model or
            # this harness, the provider is just rate-limited/quota-
            # exhausted right now. A human reviewing results later
            # needs to be able to tell "the model failed the test"
            # (F) apart from "we never got a real answer to judge"
            # (E) apart from "this is just externally throttled, try
            # again later" (Q) -- conflating any of these into the
            # same symbol would make the report actively misleading.
            wait_min = e.quota_info["wait_seconds"] / 60
            print(f" -> QUOTA: {e.quota_info['reason']} (next attempt in ~{wait_min:.0f}min, gave up waiting) {e.quota_info['message']}",
                  file=sys.stderr)
            summary_dots.append("Q")
            tier_results.append({
                "tier": tier_num, "source": tier_def["source"], "passed": False,
                "needs_manual_review": False,
                "reason": f"quota/rate-limit exhausted: {e.quota_info['reason']} -- {e.quota_info['message']}",
                "findings": {}, "session_id": session_id,
                "quota_wait_seconds": e.quota_info["wait_seconds"],
                "status_events": e.events,
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            break
        except RuntimeError as e:
            # Previously uncaught here -- one tier's HTTP/model error
            # (e.g. ProviderModelNotFoundError surfaced as an HTTP 500)
            # took down the entire eval run with a raw Python
            # traceback, losing whatever ceiling had already been
            # established by earlier tiers/categories. Report cleanly,
            # stop this category (same as a normal FAIL would), let
            # the overall run continue to the next category instead.
            print(f" -> ERROR: {e}", file=sys.stderr)
            summary_dots.append("E")
            tier_results.append({
                "tier": tier_num, "source": tier_def["source"], "passed": False,
                "needs_manual_review": False, "reason": f"HTTP/request error: {e}",
                "findings": {}, "session_id": None,
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            break

        transcript = events_to_transcript(setup_message, setup_text, setup_tools,
                                           tier_def["prompt"], probe_text, probe_tools)
        transcript_path = category_dir / f"tier{tier_num}.transcript.md"
        transcript_path.write_text(transcript)

        raw_path = category_dir / f"tier{tier_num}.raw.json"
        raw_path.write_text(json.dumps({"setup": setup_resp, "probe": probe_resp}, indent=2, default=str))

        scan_result = scan_transcript(transcript_path)
        passed, reason = check_pass(scan_result, tier_def["pass_criteria"])
        needs_review = reason.startswith("NEEDS_MANUAL_REVIEW")

        tier_record = {
            "tier": tier_num, "source": tier_def["source"], "passed": passed,
            "needs_manual_review": needs_review, "reason": reason,
            "findings": scan_result.get("category_counts", {}),
            "session_id": session_id,
            "status_events": setup_events + probe_events,
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (category_dir / f"tier{tier_num}.json").write_text(json.dumps(tier_record, indent=2))
        tier_results.append(tier_record)

        status = "PASS" if passed else ("NEEDS REVIEW" if needs_review else "FAIL")
        print(f" -> {status}: {reason}", file=sys.stderr)
        summary_dots.append("." if passed else ("R" if needs_review else "F"))

        if passed:
            ceiling = tier_num
        else:
            break

    print(f"  progress: {''.join(summary_dots)} (ceiling: tier {ceiling})", file=sys.stderr)
    return {"category": cat_id, "ceiling": ceiling, "tiers": tier_results, "progress_dots": "".join(summary_dots)}


def main() -> int:
    base_url = os.environ.get("OPENCODE_SERVER_URL", "http://server:4096")
    # 4096 is THIS project's chosen fixed port, set explicitly when
    # starting `opencode serve --port 4096 --hostname 0.0.0.0` in the
    # server container/service -- not opencode's own default. Confirmed
    # from source (cli/network.ts): opencode's real defaults are
    # port=0 (random) and hostname=127.0.0.1 (loopback only, unreachable
    # from another container). Both are overridden explicitly wherever
    # the server is started -- see docker-compose.yml / Dockerfile.
    provider = os.environ.get("OPENCODE_MODEL_PROVIDER")
    model_id = os.environ.get("OPENCODE_MODEL_ID")
    if not provider or not model_id:
        print("FATAL: OPENCODE_MODEL_PROVIDER and OPENCODE_MODEL_ID must be set", file=sys.stderr)
        return 1

    ladder_path = TASK_SUITE_DIR / "test_ladder.json"
    if not ladder_path.exists():
        print(f"FATAL: {ladder_path} not found", file=sys.stderr)
        return 1
    with open(ladder_path) as f:
        ladder = json.load(f)

    setup_message = ladder["setup_turn"]
    model_slug = f"{provider}_{model_id.replace(':', '-').replace('/', '-')}"
    results_dir = RESULTS_DIR / model_slug

    # Rotate a previous run's results before overwriting -- confirmed
    # live (real uploaded results dump) that a rerun against the same
    # model silently overwrote the prior report.json/category files in
    # place, with the only history preserved being a manual "-old"
    # rename the user did themselves. Only rotates if a previous run
    # actually completed (report.json present) -- an empty/never-used
    # directory (e.g. from mkdir with no run) isn't worth preserving.
    # Timestamp suffix, not a single "-old", so multiple past runs
    # accumulate rather than only ever keeping one generation back --
    # mirrors results/logs/'s own YYYYMMDD-HHMMSS naming rather than
    # inventing a different convention.
    if (results_dir / "report.json").exists():
        rotated_dir = results_dir.parent / f"{model_slug}.{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        print(f"[eval-client] previous results at {results_dir} found -- rotating to {rotated_dir}",
              file=sys.stderr)
        shutil.move(str(results_dir), str(rotated_dir))

    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"[eval-client] target server: {base_url}", file=sys.stderr)
    print(f"[eval-client] model under test: {provider}/{model_id}", file=sys.stderr)

    report = {"model": f"{provider}/{model_id}", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "categories": []}
    for category in ladder["categories"]:
        cat_dir = results_dir / category["id"]
        report["categories"].append(
            run_category(category, base_url, provider, model_id, setup_message, cat_dir)
        )

    (results_dir / "report.json").write_text(json.dumps(report, indent=2))

    # Capture opencode's own server-side log as an artifact -- it has
    # the REAL underlying error (e.g. ProviderModelNotFoundError)
    # behind a generic client-visible HTTP 500 wrapper. NOT a
    # "previously invisible" fix -- --print-logs (entrypoint.sh)
    # already mirrors this to stderr, so `docker logs server` shows it
    # live. What this actually adds: docker logs belongs to the
    # daemon, tied to the server container specifically -- this
    # (eval/discover) container has no access to it at all, and it
    # isn't scoped to any one run. This makes it a per-run file
    # artifact living alongside report.json instead.
    #
    # Filtered by this run's own session IDs and error refs (see
    # extract_error_refs/filter_log_by_identifiers) -- NOT a raw whole-
    # file copy. Closes the earlier scope gap directly: the server's
    # log accumulates every run's history for as long as it stays up,
    # and a previous version of this capture step copied the whole
    # thing unfiltered, meaning results from run N would include every
    # other run's interleaved lines too. Falls back to the whole file
    # only if this run produced zero identifiers to filter by at all
    # (see filter_log_by_identifiers's own fallback).
    log_identifiers = set()
    for cat_report in report["categories"]:
        for tier in cat_report["tiers"]:
            if tier.get("session_id"):
                log_identifiers.add(tier["session_id"])
            log_identifiers |= extract_error_refs(tier.get("reason", ""))

    try:
        if OPENCODE_LOG_PATH.exists():
            full_log = OPENCODE_LOG_PATH.read_text(errors="replace")
            filtered_log = filter_log_by_identifiers(full_log, log_identifiers)
            (results_dir / "server.log").write_text(filtered_log)
        else:
            print(f"[eval-client] NOTE: {OPENCODE_LOG_PATH} not found -- "
                  "server.log artifact not captured. Confirm the opencode-log "
                  "volume is mounted (see docker-compose.yml/terraform).",
                  file=sys.stderr)
    except OSError as e:
        # Never let a log-capture failure take down an otherwise-
        # successful run -- this is a nice-to-have artifact, not
        # something the eval run's actual correctness depends on.
        print(f"[eval-client] NOTE: failed to capture server.log: {e}", file=sys.stderr)

    print(f"\n=== Summary (model: {provider}/{model_id}) ===", file=sys.stderr)
    for cat_report in report["categories"]:
        last_tier = cat_report["tiers"][-1] if cat_report["tiers"] else None
        note = ""
        if last_tier and not last_tier["passed"]:
            if last_tier.get("needs_manual_review"):
                note = " [stopped: NEEDS MANUAL REVIEW]"
            elif last_tier.get("reason", "").startswith("quota/rate-limit exhausted"):
                wait_min = last_tier.get("quota_wait_seconds", 0) / 60
                note = f" [stopped: QUOTA -- next opencode attempt in ~{wait_min:.0f}min, gave up waiting]"
            elif last_tier.get("reason", "").startswith("HTTP/request error"):
                note = " [stopped: ERROR]"
            else:
                note = " [stopped: CVV violation]"
        print(f"  {cat_report['category']}: ceiling tier {cat_report['ceiling']}{note}", file=sys.stderr)

    # Compact grid, all categories aligned -- the "at a glance, what
    # happened across the whole run" view. . pass / F fail / R needs
    # review / E request error. Category names padded to the longest
    # one so the dot-columns line up.
    print(f"\n=== Progress grid ===", file=sys.stderr)
    name_width = max(len(c["category"]) for c in report["categories"]) if report["categories"] else 0
    for cat_report in report["categories"]:
        name = cat_report["category"].ljust(name_width)
        print(f"  {name} : {cat_report.get('progress_dots', '')}", file=sys.stderr)

    print(f"\nFull report: {results_dir / 'report.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
