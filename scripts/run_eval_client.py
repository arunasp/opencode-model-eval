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
import signal
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
# Direct request: a genuine client-side socket timeout ("timed out
# after Ns waiting for a response") previously went straight to "E"
# with zero retry -- distinct from quota exhaustion (already has its
# own bounded-wait/give-up logic) and from a deterministic failure
# like ContextOverflowError (retrying that would just fail identically,
# wasting another full timeout window). 1 retry by default -- a second
# genuine timeout in a row is much more likely a real, persistent
# problem than a transient blip worth a third attempt.
TIER_TIMEOUT_RETRY_LIMIT = int(os.environ.get("OPENCODE_TIER_TIMEOUT_RETRY_LIMIT", "1"))
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


class _ServerLogError(Exception):
    """Internal signal, not a real error -- same pattern as
    _QuotaExhausted, raised inside run_category() when
    quota_aware_send_message()'s polling loop finds a matching ERROR
    line in opencode's own server log for this session, rather than
    waiting for our own client-side socket timeout to expire. Direct
    request: the client already has full access to this log (used for
    the end-of-run capture and the interrupt handler) -- it should use
    it DURING the wait too, not just after. Confirmed live: a real
    "exceeds the available context size" failure showed up in the
    server log almost instantly, while our own client sat blind for
    the full 300s waiting on a socket read that opencode's own internal
    compaction-retry loop was never going to satisfy. Deliberately NOT
    retried by run_category() -- the specific failure this closes
    (context-size mismatch) is deterministic, and a genuinely different
    class of server-log error is unverified/untested territory, so
    failing fast with the precise message is the safe default until
    there's real evidence a broader retry policy is warranted.
    """
    def __init__(self, message: str, events: list[dict]):
        self.message = message
        self.events = events
        super().__init__(message)


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


def send_message(base_url: str, session_id: str, provider: str, model_id: str, text: str,
                  timeout: int = 300) -> dict:
    body = {
        "model": {"providerID": provider, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    return http_post(base_url, f"/session/{session_id}/message", body, timeout=timeout)


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

        log_error_line = _check_session_log_error(session_id)
        if log_error_line is not None:
            # Direct request: use the log DURING the wait, not just
            # after -- confirmed live, a real context-size-exceeded
            # failure was visible in the server log almost instantly,
            # while our own client would otherwise sit blind for the
            # full client-side timeout waiting on a socket read that
            # opencode's own internal retry/compaction loop was never
            # going to satisfy. Same abort-then-give-the-worker-a-
            # window pattern as the quota bailout below.
            try:
                abort_session(base_url, session_id)
            except RuntimeError:
                pass  # best-effort -- we're bailing on this tier regardless
            done_event.wait(timeout=poll_interval_s)
            events.append({"timestamp": time.time(), "type": "server_log_error", "line": log_error_line})
            return None, {"kind": "server_log_error", "message": log_error_line}, events

        status_type = status.get("type")
        now = time.time()
        if status_type != last_status_type:
            events.append({"timestamp": now, **status})
            print(f"[eval-client] status changed: {last_status_type} -> {status_type} "
                  f"(elapsed {now - wait_start:.0f}s, {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))})",
                  flush=True)
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
                  f"threshold {quota_wait_threshold_s:.0f}s, "
                  f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))})", flush=True)
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
                        "kind": "quota",
                        "reason": action.get("reason", "unknown"),
                        "wait_seconds": wait_s,
                        "message": status.get("message", ""),
                    }, events

    if "value" in exception_holder:
        # Best-effort abort so this session isn't left orphaned,
        # retrying forever server-side. Confirmed live: this was the
        # missing case -- any exception OTHER than the explicit
        # quota-bailout above (e.g. our own http_post timeout) reached
        # here and just re-raised, leaving the session opencode
        # created still alive with nothing telling it to stop. Since
        # opencode's own internal retry is confirmed unbounded, it
        # kept retrying indefinitely -- this is what accumulates into
        # the "errors even while idle" symptom across every run that
        # ever hit this path.
        try:
            abort_session(base_url, session_id)
        except RuntimeError:
            pass  # best-effort -- we're already raising the original error below
        raise exception_holder["value"]
    return result_holder.get("value"), None, events


def extract_reply(response: dict) -> tuple[str, list[dict]]:
    """Extracts (assistant_text, tool_calls) from a SessionV1.WithParts
    response. Primary path (top-level "parts", filter on type=="text")
    confirmed empirically against a real opencode serve instance -- see
    module docstring. The message.parts fallback and the tool-call
    branch remain defensive/inferred, not exercised by that same test.

    Raises RuntimeError if info.finish == "error" -- confirmed live:
    a real ContextOverflowError response (VibeThinker-3B's context
    window too small even after opencode's own auto-compaction) came
    back with parts: [] and no exception from send_message() itself
    (the HTTP call succeeded; the model/compaction step inside it
    failed). Silently returning empty text here previously let this
    flow straight through to check_pass()/scan_transcript() against an
    empty transcript, which found zero CVV violations by definition
    and scored the tier a clean PASS -- an opencode-level error being
    reported as if the model had answered cleanly. Raising here instead
    routes it into run_category()'s existing RuntimeError handler,
    which already marks a tier "E" (error, not a capability failure)
    for exactly this kind of "we got a response but can't score it"
    case -- reusing established semantics, not inventing new ones.
    """
    info = response.get("info", {})
    if isinstance(info, dict) and info.get("finish") == "error":
        error = info.get("error", {})
        name = error.get("name", "unknown error") if isinstance(error, dict) else "unknown error"
        message = (
            error.get("data", {}).get("message", "") if isinstance(error, dict) else ""
        )
        raise RuntimeError(f"opencode returned an error response: {name}: {message}")

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


def _check_session_log_error(session_id: str) -> str | None:
    """Checks opencode's own server log for a "level=ERROR" line
    already mentioning this session -- best-effort, returns None on
    any failure (missing file, read error, etc.) rather than raising,
    since this is purely an early-detection optimization layered on
    top of the existing timeout-based give-up, not a replacement for
    it. Reuses filter_log_by_identifiers' own substring-matching logic
    (same reasoning: session IDs appear under different field names
    across different log line shapes) rather than a fresh parser.
    """
    try:
        if not OPENCODE_LOG_PATH.exists():
            return None
        full_log = OPENCODE_LOG_PATH.read_text(errors="replace")
    except OSError:
        return None
    session_lines = filter_log_by_identifiers(full_log, {session_id})
    for line in session_lines.splitlines():
        if "level=ERROR" in line:
            return line
    return None


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
    cat_start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[eval-client] category: {cat_id}: {category['description']} (started {cat_start_utc})",
          file=sys.stderr)
    category_dir.mkdir(parents=True, exist_ok=True)
    ceiling = 0
    tier_results = []
    summary_dots = []  # one char per tier: . pass / F fail / R review / E error / Q quota-exhausted -- printed as a compact row at the end of this category, and again in main()'s final grid

    for tier_def in category["tiers"]:
        tier_num = tier_def["tier"]
        # Progress within a single tier, not just between tiers: a
        # single slow LLM response previously looked identical to a
        # hung process from the CLI's perspective (nothing printed
        # until the whole tier finished). A flushed, timestamped marker
        # per HTTP
        # round-trip instead -- visible movement during exactly the
        # kind of multi-minute single-request wait that prompted this.
        print(f"  [tier {tier_num}] source={tier_def['source']} ", end="", file=sys.stderr, flush=True)

        tier_t0 = time.monotonic()

        def _elapsed_marker(label: str) -> str:
            return f"[{label}:+{time.monotonic() - tier_t0:.1f}s]"

        # Direct request: a genuine client-side socket timeout (the
        # exact "timed out after Ns waiting for a response" message --
        # confirmed this means http_post()'s own read timeout fired
        # with truly no bytes back, not a parsing bug: a parse failure
        # would show a JSONDecodeError instead, with real bytes having
        # arrived) previously went straight to "E" with no retry at
        # all. quota_aware_send_message()'s own docstring already
        # explains why this class of retry doesn't duplicate opencode's
        # internal one: that's for provider-side rate-limiting
        # (session/retry.ts), which can't help with a raw socket
        # timeout on OUR OWN connection to opencode itself -- a
        # genuinely different failure this project's own client needs
        # to handle. Deliberately NOT a blanket retry-everything: a
        # deterministic failure like ContextOverflowError would just
        # fail identically on retry, wasting another full timeout
        # window for nothing -- only retried when the message
        # specifically indicates a timeout.
        timeout_retries_left = TIER_TIMEOUT_RETRY_LIMIT
        stop_category = False
        while True:
            session_id = None
            try:
                session_id = create_session(base_url)
                _CURRENT_RUN_STATE["session_id"] = session_id
                print(_elapsed_marker("session"), end="", file=sys.stderr, flush=True)
                setup_resp, quota_info, setup_events = quota_aware_send_message(
                    base_url, session_id, provider, model_id, setup_message)
                if quota_info is not None:
                    if quota_info.get("kind") == "server_log_error":
                        raise _ServerLogError(quota_info["message"], setup_events)
                    raise _QuotaExhausted(quota_info, setup_events)
                print(_elapsed_marker("setup"), end="", file=sys.stderr, flush=True)
                setup_text, setup_tools = extract_reply(setup_resp)
                probe_resp, quota_info, probe_events = quota_aware_send_message(
                    base_url, session_id, provider, model_id, tier_def["prompt"])
                if quota_info is not None:
                    if quota_info.get("kind") == "server_log_error":
                        raise _ServerLogError(quota_info["message"], setup_events + probe_events)
                    raise _QuotaExhausted(quota_info, setup_events + probe_events)
                print(_elapsed_marker("probe"), end="", file=sys.stderr, flush=True)
                probe_text, probe_tools = extract_reply(probe_resp)
                # Both round-trips succeeded -- close this tier's session now,
                # regardless of what check_pass() below judges it as. This was
                # the missing case: quota_aware_send_message() already aborts
                # on a quota-bailout (its own "retry" threshold branch) and on
                # any raw exception from send_message() itself, but a tier that
                # completes normally -- the common case for every PASS and
                # every judged FAIL -- fell through both of those and was never
                # aborted at all, leaving opencode holding the session (and,
                # for local/ollama, the model) open indefinitely. Confirmed
                # live: this is what kept Ollama persistently resident even
                # after the eval-client process producing the load was gone.
                try:
                    abort_session(base_url, session_id)
                except RuntimeError:
                    pass  # best-effort -- we already have the data we need
                _CURRENT_RUN_STATE["session_id"] = None
                break  # success -- fall through to scoring below
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
                print(f" -> QUOTA ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): "
                      f"{e.quota_info['reason']} (next attempt in ~{wait_min:.0f}min, gave up waiting) "
                      f"{e.quota_info['message']}",
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
                stop_category = True
                break
            except _ServerLogError as e:
                # Detected via opencode's OWN server log, faster and
                # more precise than waiting for our own client-side
                # socket timeout to expire -- direct request: the
                # client already has full access to this log (used for
                # the end-of-run capture and the interrupt handler),
                # it should use it DURING the wait too. Deliberately
                # NOT retried (unlike a genuine socket timeout below):
                # the specific failure this closes (a context-size
                # mismatch) is deterministic, and a genuinely different
                # class of server-log error is unverified territory --
                # failing fast with the precise message is the safe
                # default.
                print(f" -> SERVER ERROR ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): "
                      f"{e.message}", file=sys.stderr)
                summary_dots.append("E")
                tier_results.append({
                    "tier": tier_num, "source": tier_def["source"], "passed": False,
                    "needs_manual_review": False,
                    "reason": f"opencode server log error: {e.message}",
                    "findings": {}, "session_id": None,
                    "status_events": e.events,
                    "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                stop_category = True
                break
            except RuntimeError as e:
                is_timeout = "timed out after" in str(e)
                if session_id is not None:
                    try:
                        abort_session(base_url, session_id)
                    except RuntimeError:
                        pass
                    _CURRENT_RUN_STATE["session_id"] = None
                if is_timeout and timeout_retries_left > 0:
                    timeout_retries_left -= 1
                    print(f" -> TIMEOUT ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): {e} "
                          f"-- retrying with a fresh session ({timeout_retries_left} retr"
                          f"{'y' if timeout_retries_left == 1 else 'ies'} left)", file=sys.stderr)
                    print(f"  [tier {tier_num}] retry ", end="", file=sys.stderr, flush=True)
                    tier_t0 = time.monotonic()
                    continue
                # Previously uncaught here -- one tier's HTTP/model error
                # (e.g. ProviderModelNotFoundError surfaced as an HTTP 500)
                # took down the entire eval run with a raw Python
                # traceback, losing whatever ceiling had already been
                # established by earlier tiers/categories. Report cleanly,
                # stop this category (same as a normal FAIL would), let
                # the overall run continue to the next category instead.
                print(f" -> ERROR ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): {e}", file=sys.stderr)
                summary_dots.append("E")
                tier_results.append({
                    "tier": tier_num, "source": tier_def["source"], "passed": False,
                    "needs_manual_review": False, "reason": f"HTTP/request error: {e}",
                    "findings": {}, "session_id": None,
                    "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                stop_category = True
                break

        if stop_category:
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

    cat_end_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"  progress: {''.join(summary_dots)} (ceiling: tier {ceiling}, finished {cat_end_utc})",
          file=sys.stderr)
    return {"category": cat_id, "ceiling": ceiling, "tiers": tier_results, "progress_dots": "".join(summary_dots)}


WARMUP_TIMEOUT_S = float(os.environ.get("OPENCODE_WARMUP_TIMEOUT_S", "600"))  # 10 min default
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
# Reached directly from THIS container, not through opencode's `server`
# -- separate concern from OPENCODE_SERVER_URL above. Needs
# host.docker.internal wired in (see docker-compose.yml's eval service
# / tf-select-and-run-eval.sh's --add-host) since this container
# otherwise only reaches `server`.
OLLAMA_PS_POLL_INTERVAL_S = float(os.environ.get("OPENCODE_OLLAMA_PS_POLL_INTERVAL_S", "15"))
OLLAMA_UNLOAD_TIMEOUT_S = float(os.environ.get("OPENCODE_OLLAMA_UNLOAD_TIMEOUT_S", "300"))


def ollama_ps(ollama_base_url: str) -> list[dict]:
    """GET /api/ps -- currently loaded Ollama models. Same endpoint
    scripts/ollama-model-switch.sh already uses from the host side
    (verified there against Ollama's own documented API,
    docs.ollama.com/api/ps) -- this is the container-side equivalent,
    reached via host.docker.internal instead of localhost. Best-effort:
    returns [] on any failure rather than raising, since every caller
    here treats this as informational/best-effort, never load-bearing.
    """
    try:
        req = urllib.request.Request(f"{ollama_base_url}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("models", [])
    except Exception:
        return []


def _ollama_model_entry(model_id: str, models: list[dict]) -> dict | None:
    for m in models:
        if m.get("name") == model_id or m.get("model") == model_id:
            return m
    return None


def _poll_ollama_ps_during(ollama_base_url: str, model_id: str, done_event: threading.Event,
                            poll_interval_s: float = OLLAMA_PS_POLL_INTERVAL_S) -> None:
    """Runs in a background thread alongside a blocking call, printing
    periodic /api/ps status purely for visibility -- NOT a gate.
    Confirmed against Ollama's real, documented API schema that
    /api/ps has no busy/processing field, only load/unload residency
    -- so this can tell you "is the model resident", not "is it still
    draining a previous request". Exists so a long wait is
    distinguishable from a hang, same philosophy as
    scripts/ollama-model-switch.sh's own /api/ps polling on the host
    side.

    Confirmed live and via multiple independent sources (Ollama's own
    docs.ollama.com/api/ps plus several third-party references, all
    consistent): the real JSON field names are size_vram/size (VRAM
    residency) and expires_at (ISO8601 timestamp) -- there is no field
    literally named "processor" or "until" at all. Those are only the
    CLI's own display column labels (ollama/ollama#4840 also confirms
    size_vram is sometimes OMITTED from the response entirely, not
    just zero, when nothing's offloaded to GPU -- handled below via
    .get() with a 0 default, not an assumed-present key).
    """
    start = time.time()
    while not done_event.wait(timeout=poll_interval_s):
        elapsed = time.time() - start
        entry = _ollama_model_entry(model_id, ollama_ps(ollama_base_url))
        if entry is not None:
            total_size = entry.get("size", 0)
            vram_size = entry.get("size_vram", 0)
            if total_size > 0:
                vram_pct = round(vram_size / total_size * 100)
                if vram_pct >= 100:
                    processor = "100% GPU"
                elif vram_pct <= 0:
                    processor = "100% CPU"
                else:
                    processor = f"{vram_pct}% GPU/{100 - vram_pct}% CPU"
            else:
                processor = "?"
            expires_at = entry.get("expires_at", "?")
            status = f"loaded (processor={processor}, until={expires_at})"
        else:
            status = "not loaded"

        dispatched_at = _WARMUP_REQUEST_STATE["dispatched_at"]
        if dispatched_at is None:
            request_status = "\"hi\" not yet sent"
        else:
            request_status = f'"hi" outstanding for {time.time() - dispatched_at:.0f}s'

        print(f"[eval-client] ollama /api/ps (elapsed {elapsed:.0f}s, "
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}): {status}, {request_status}",
              file=sys.stderr)


def warm_up_local_model(base_url: str, provider: str, model_id: str) -> None:
    """Sends one throwaway message to force Ollama to load the model
    into memory BEFORE the real test ladder starts, so a cold-start
    load (Ollama's documented default: unloads a model 5min after its
    last use, then the next request pays the full weight-load time
    before generating anything) doesn't eat into a real tier's own
    300s budget. Confirmed live: a fresh local/ollama run's very first
    tier timed out at exactly 300s with the server reachable and the
    session genuinely busy the whole time -- consistent with cold-load
    time alone consuming the budget, not a connectivity problem.

    A background thread prints periodic /api/ps status for visibility
    while this blocks (see _poll_ollama_ps_during's docstring for why
    that's informational only, not a gate) -- bounded by the same
    WARMUP_TIMEOUT_S hard timeout the blocking call itself has.

    Best-effort and silent on failure -- if warm-up itself times out or
    errors, that's not fatal here; the real test ladder will surface
    the same underlying problem with proper category/tier context
    instead. Not called for cloud providers -- there's no cold-load
    concept for a hosted API, and doing this unconditionally would
    waste a request/tokens on every cloud run for no benefit.
    """
    if provider != "local/ollama":
        return
    print(f"[eval-client] warming up {provider}/{model_id} before the test ladder "
          f"(hard timeout {WARMUP_TIMEOUT_S:.0f}s -- Ollama cold-start on a large "
          f"model can take a while)...", file=sys.stderr)

    done_event = threading.Event()
    poller = threading.Thread(
        target=_poll_ollama_ps_during,
        args=(OLLAMA_BASE_URL, model_id, done_event),
        daemon=True,
    )
    poller.start()

    session_id = None
    try:
        session_id = create_session(base_url)
        _CURRENT_RUN_STATE["session_id"] = session_id
        _WARMUP_REQUEST_STATE["dispatched_at"] = time.time()
        send_message(base_url, session_id, provider, model_id, "hi", timeout=int(WARMUP_TIMEOUT_S))
        print("[eval-client] warm-up complete", file=sys.stderr)
    except RuntimeError as e:
        print(f"[eval-client] warm-up failed/timed out at the {WARMUP_TIMEOUT_S:.0f}s hard "
              f"timeout ({e}) -- proceeding to the real test ladder anyway, which will "
              f"surface the same problem with proper category/tier context if it's genuine",
              file=sys.stderr)
    finally:
        done_event.set()
        poller.join(timeout=5)
        _WARMUP_REQUEST_STATE["dispatched_at"] = None
        if session_id is not None:
            try:
                abort_session(base_url, session_id)
            except RuntimeError:
                pass  # best-effort -- warm-up is already done/failed regardless
            _CURRENT_RUN_STATE["session_id"] = None


def unload_local_model(ollama_base_url: str, model_id: str,
                        timeout_s: float = OLLAMA_UNLOAD_TIMEOUT_S,
                        poll_interval_s: float = OLLAMA_PS_POLL_INTERVAL_S) -> None:
    """Explicitly unloads the model from Ollama once the run finishes,
    via the same native API scripts/ollama-model-switch.sh already
    uses (POST /api/generate {"model":..., "keep_alive":0}) instead of
    just letting Ollama's own 5min idle keep-alive expire naturally --
    frees GPU/CPU memory right away for whatever runs next.

    Unlike the warm-up poller above, /api/ps polling here DOES
    meaningfully gate: presence/absence in /api/ps is exactly
    load/unload residency (the thing this function is actually
    changing), not the busy-state /api/ps can't see. Bounded by a hard
    timeout (OPENCODE_OLLAMA_UNLOAD_TIMEOUT_S, 300s default) -- gives
    up the wait past that point rather than blocking the whole run's
    completion on it, since Ollama's own keep-alive will expire it
    eventually regardless.

    Best-effort throughout: this runs after the real test ladder is
    already done, so nothing downstream depends on it succeeding --
    any failure is logged, never raised.
    """
    print(f"[eval-client] unloading {model_id} from Ollama (hard timeout {timeout_s:.0f}s)...",
          file=sys.stderr)
    try:
        body = json.dumps({"model": model_id, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(f"{ollama_base_url}/api/generate", data=body,
                                      headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[eval-client] unload request failed ({e}) -- Ollama's own keep-alive "
              f"will still expire it eventually", file=sys.stderr)
        return

    start = time.time()
    while time.time() - start < timeout_s:
        if _ollama_model_entry(model_id, ollama_ps(ollama_base_url)) is None:
            print(f"[eval-client] unload confirmed after {time.time() - start:.0f}s", file=sys.stderr)
            return
        print(f"[eval-client] still unloading (elapsed {time.time() - start:.0f}s)...", file=sys.stderr)
        time.sleep(poll_interval_s)
    print(f"[eval-client] unload not confirmed within the {timeout_s:.0f}s hard timeout -- "
          f"giving up the wait (Ollama's own keep-alive will still expire it eventually)",
          file=sys.stderr)


# Confirmed live: hitting Ctrl-C during a run raises KeyboardInterrupt
# straight through main()'s call stack with nothing caught anywhere --
# unload_local_model() (only ever called at the very end of a normal
# run) never runs, and whatever tier's session was in flight is never
# aborted either. Real log evidence: `ollama ps` showed the model
# still resident with a live 5min keep_alive countdown well after the
# interrupt. This module-level dict + signal handler close that gap --
# updated as the run progresses (main() sets base_url/provider/model_id
# once; run_category() updates session_id per tier) so the handler has
# something to clean up regardless of exactly where execution was when
# the signal arrived.
_CURRENT_RUN_STATE = {
    "base_url": None,
    "session_id": None,
    "provider": None,
    "model_id": None,
    "results_dir": None,
}

# Confirmed live: two genuinely different scenarios during warm-up look
# identical in the poller's output without this -- a large model still
# loading into VRAM (Ollama's own /api/ps residency hasn't flipped to
# loaded yet) versus a light model that loaded almost instantly but
# whose actual "hi" generation is itself just slow (residency shows
# loaded immediately, but the real bottleneck is generation, not
# loading). _poll_ollama_ps_during() only ever showed Ollama's own
# residency state, with nothing about whether the real "hi" round-trip
# was even dispatched yet, let alone how long it's been outstanding.
# warm_up_local_model() sets "dispatched_at" right before calling
# send_message(); the poller (a separate thread) reads it to report
# both signals side by side instead of conflating them.
_WARMUP_REQUEST_STATE = {"dispatched_at": None}

_INTERRUPT_HANDLING = False  # re-entrancy guard -- a second signal while cleaning up shouldn't restart the cleanup


def _handle_interrupt(signum: int, frame) -> None:
    global _INTERRUPT_HANDLING
    if _INTERRUPT_HANDLING:
        # Second interrupt during cleanup -- stop trying, just exit.
        sys.exit(130)
    _INTERRUPT_HANDLING = True

    sig_name = signal.Signals(signum).name
    print(f"\n[eval-client] {sig_name} received -- best-effort cleanup before exiting "
          f"(session abort + local model unload if applicable, both short-timeout so this "
          f"doesn't hang)...", file=sys.stderr)

    base_url = _CURRENT_RUN_STATE["base_url"]
    session_id = _CURRENT_RUN_STATE["session_id"]
    if base_url and session_id:
        try:
            abort_session(base_url, session_id)
            print(f"[eval-client] aborted in-flight session {session_id}", file=sys.stderr)
        except Exception as e:
            print(f"[eval-client] session abort on interrupt failed (non-fatal): {e}", file=sys.stderr)

    provider = _CURRENT_RUN_STATE["provider"]
    model_id = _CURRENT_RUN_STATE["model_id"]
    if provider == "local/ollama" and model_id:
        try:
            # Short timeout here on purpose -- unload_local_model()'s
            # normal call (end of a successful run) blocks up to
            # OLLAMA_UNLOAD_TIMEOUT_S (300s default) polling for
            # confirmation, which is fine when the run's already done
            # but wrong here: someone who just hit Ctrl-C wants the
            # process to actually exit promptly, not wait another 5
            # minutes. Fires the keep_alive:0 request and gives up
            # waiting for confirmation quickly -- Ollama's own
            # keep_alive will still expire it soon regardless.
            unload_local_model(OLLAMA_BASE_URL, model_id, timeout_s=5)
        except Exception as e:
            print(f"[eval-client] model unload on interrupt failed (non-fatal): {e}", file=sys.stderr)

    # Confirmed live: an interrupted run previously left ZERO diagnostic
    # artifacts behind -- results/logs/ is only ever written by
    # harness-control.sh's own run_logged() wrapper (out of scope for a
    # direct script invocation like this), and results/<model>/server.log
    # is only ever captured at the very end of a NORMAL run, well after
    # this interrupt path already returned. Best-effort raw copy here,
    # deliberately UNFILTERED (unlike the normal end-of-run capture,
    # which filters by this run's own session IDs/error refs) -- we
    # don't reliably have a full set of identifiers to filter by mid-
    # interrupt, and an unfiltered dump for the rare interrupted case is
    # far more useful than the alternative of capturing nothing at all.
    # Separate filename from the normal "server.log" so it's obvious at
    # a glance which capture mode produced a given file.
    results_dir = _CURRENT_RUN_STATE["results_dir"]
    if results_dir is not None:
        try:
            if OPENCODE_LOG_PATH.exists():
                results_dir.mkdir(parents=True, exist_ok=True)
                (results_dir / "server.log.interrupted").write_text(
                    OPENCODE_LOG_PATH.read_text(errors="replace")
                )
                print(f"[eval-client] captured (unfiltered) server log to "
                      f"{results_dir / 'server.log.interrupted'}", file=sys.stderr)
            else:
                print(f"[eval-client] NOTE: {OPENCODE_LOG_PATH} not found -- "
                      "no server log to capture on interrupt", file=sys.stderr)
        except OSError as e:
            print(f"[eval-client] server log capture on interrupt failed (non-fatal): {e}",
                  file=sys.stderr)

    sys.exit(130)  # 128 + SIGINT(2), the conventional exit code for this -- matches what was already observed


signal.signal(signal.SIGINT, _handle_interrupt)
signal.signal(signal.SIGTERM, _handle_interrupt)


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

    _CURRENT_RUN_STATE["base_url"] = base_url
    _CURRENT_RUN_STATE["provider"] = provider
    _CURRENT_RUN_STATE["model_id"] = model_id

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
    # rename the user did themselves. Triggers on ANY prior content in
    # results_dir, not just a complete run's report.json -- confirmed
    # live a second time: an INTERRUPTED run (Ctrl-C mid-category,
    # caught by the new SIGINT handler but still genuinely incomplete)
    # never reaches the report.json write at all, so the old
    # report.json-only check silently overwrote that partial run's
    # category/tier files on the next attempt against the same model,
    # with no rotation. any(results_dir.iterdir()) is true for a
    # directory holding even one category folder, complete run or not.
    # Timestamp suffix, not a single "-old", so multiple past runs
    # accumulate rather than only ever keeping one generation back --
    # mirrors results/logs/'s own YYYYMMDD-HHMMSS naming rather than
    # inventing a different convention.
    if results_dir.exists() and any(results_dir.iterdir()):
        rotated_dir = results_dir.parent / f"{model_slug}.{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        print(f"[eval-client] previous results at {results_dir} found -- rotating to {rotated_dir}",
              file=sys.stderr)
        shutil.move(str(results_dir), str(rotated_dir))

    results_dir.mkdir(parents=True, exist_ok=True)
    _CURRENT_RUN_STATE["results_dir"] = results_dir

    print(f"[eval-client] target server: {base_url}", file=sys.stderr)
    print(f"[eval-client] model under test: {provider}/{model_id}", file=sys.stderr)

    warm_up_local_model(base_url, provider, model_id)

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

    if provider == "local/ollama":
        unload_local_model(OLLAMA_BASE_URL, model_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
