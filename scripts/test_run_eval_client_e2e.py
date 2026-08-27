#!/usr/bin/env python3
"""test_run_eval_client_e2e.py -- REAL end-to-end test.

Not a unit test with a mock standing in for opencode. This installs
the actual `opencode-ai` npm package, runs the real `opencode serve`
binary as a subprocess pointed at scripts/tools/mock_openai_backend.py
(also a real, separately-running process, not an in-process mock), and
drives it through run_eval_client.py's own create_session/send_message/
extract_reply() functions -- the same code path the harness uses
against real models.

This is what actually backs the "RESPONSE SCHEMA -- CONFIRMED
empirically" claim in run_eval_client.py's docstring. That claim was
previously made in a prior session with no committed test behind it --
this closes that gap.

WHY THIS TEST HUNG FOR FIVE WEEKS, and what it actually was. Two
hypotheses lived here before, both hedged as unconfirmed and both
wrong: a blocked outbound call from opencode's own startup path, and a
cold npm cache pushing session creation past the deadline. Neither was
ever tested, and the label became the premise every later reader
reasoned from -- including three more wrong guesses on 2026-08-27
before anyone measured it.

MEASURED, 2026-08-27. An open TCP port is not a ready server.
opencode's listener accepts connections roughly 1.5s before its route
layer can serve, and a request landing inside that window is accepted,
has its bytes drained out of the kernel receive buffer, and is then
never answered -- not answered late. The process sits at ~1% of a core
meanwhile and its own log records nothing, because the request never
reaches a handler. Post 0s after the port opens: blocked past 40s,
every time. Post 5s after: HTTP 200 in ~120ms. Reproduced identically
on 1.18.3 and 1.18.23, so it is not version-specific, and the harness's
own long-running server never shows it because real requests arrive
long after startup.

The original commit (380f459) observed the right thing and drew the
wrong conclusion from it: "the mock backend's request log stays
completely empty -- opencode never reaches the configured provider" is
exactly what a request wedged before reaching a handler looks like. It
was read as a network block instead. Nothing about the network was ever
at fault; models.dev IS unreachable from a restricted environment (403,
confirmed in both a worker and a sandbox) and is irrelevant to this.

_wait_until_serving() below is the fix, and it is why this test now
passes at all. If it ever hangs again, the cause is NOT this: check
whether the readiness wait returned, then read opencode's own log at
$HOME/.local/share/opencode/log/opencode.log -- the diagnosis has
appeared there every time and this test used to discard it, printing
only the subprocess pipe, which carries two lines.

Requires: node + npm on PATH, and network access to the npm registry
(registry.npmjs.org) to install opencode-ai on first run.

Usage:
    python3 scripts/test_run_eval_client_e2e.py
    (skips with a clear message if node/npm aren't on PATH)
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Pinned to the current stable release. It was previously 1.18.3 -- the
# version the original schema claim was made against -- so this suite
# verified a response schema for a build nobody ran, twenty releases
# behind. Bump this and the image's OPENCODE_REF together; a suite that
# tests a different build than it ships is testing nothing useful.
OPENCODE_VERSION = "1.18.23"
SERVER_STARTUP_TIMEOUT_S = 15
# Confirmed live via cicd-runner (2026-08-19): a cold run (fresh npm
# cache, first invocation on a new worker) took 37.966s wall-clock for
# both tests and failed the second one at exactly this deadline, with
# opencode.ai confirmed REACHABLE the whole time (direct TCP check) --
# a genuinely different cause than the module docstring's originally-
# documented network-block hypothesis, which was written in a more
# network-restricted sandbox. An immediate re-run (warm npm cache) hit
# 12.126s for both and passed cleanly. 30s (was 20s) gives real
# cold-start headroom without masking the case this test actually
# exists to catch -- a genuine indefinite hang still exceeds any finite
# bound. Both causes (network block, cold-start timing) remain live
# possibilities on a real failure; check reachability first (fast to
# rule out), then consider whether this was the environment's first
# invocation.
SESSION_REQUEST_TIMEOUT_S = 30


# MEASURED 2026-08-27, and it retires both hypotheses in the docstring
# above. An open TCP port is not a ready server: opencode's listener
# accepts connections roughly 1.5s before its route layer can serve, and
# a request landing inside that window is accepted, has its bytes drained
# out of the kernel receive buffer, and is then never answered at all --
# not answered late. The process sits at ~1% of a core while it happens,
# and its own log records nothing, because the request never reaches a
# handler. Post 0s after the port opens: blocked past 40s, every time.
# Post 5s after: HTTP 200 in ~120ms. Reproduced identically on 1.18.3 and
# 1.18.23, so it is not version-specific, and the harness's own
# long-running server never shows it because real requests arrive long
# after startup.
#
# That is why raising SESSION_REQUEST_TIMEOUT_S never helped and why
# reaping the subprocess did not move the hang rate: the request is
# wedged, so no finite deadline can catch it. Two attempts is the
# measured cost of readiness -- the first times out in the window, the
# second answers.
READY_ATTEMPT_TIMEOUT_S = 2
READY_DEADLINE_S = 60


def _wait_until_serving(base_url: str, deadline_s: int = READY_DEADLINE_S):
    """Block until the server answers an HTTP request, or give up.

    Any HTTP status counts, including an error: this establishes that the
    route layer is running, not that the endpoint is happy. Each attempt
    carries its own short timeout so an attempt made inside the dead
    window is abandoned rather than waited on -- abandoned attempts are
    cheap here because the server is idle while wedged, and it closes
    them itself.

    Returns (seconds_waited, attempts), or (None, attempts) on timeout.
    """
    started = time.monotonic()
    attempts = 0
    while time.monotonic() - started < deadline_s:
        attempts += 1
        try:
            urllib.request.urlopen(f"{base_url}/session", timeout=READY_ATTEMPT_TIMEOUT_S)
            return round(time.monotonic() - started, 2), attempts
        except urllib.error.HTTPError:
            return round(time.monotonic() - started, 2), attempts
        except Exception:
            time.sleep(0.5)
    return None, attempts


def _kill_and_reap(proc: subprocess.Popen) -> None:
    """Kill the server AND wait for it, so the process is reaped.

    `proc.kill()` alone only sends the signal. Without a wait the child
    is left unreaped and Python reports
    `ResourceWarning: subprocess N is still running` at interpreter
    exit -- observed in a real pipeline log. An unreaped server can
    still hold its listening socket briefly, which is a candidate
    explanation for this suite's intermittent hang at create_session
    (9 hangs in 14 recorded runs): the next run binds a fresh port, but
    a lingering process competing for local resources fits the
    alternating pass/fail pattern better than a fixed environment
    block. NOT CONFIRMED as the cause -- reaping is correct regardless,
    and if the hang rate does not move afterwards the hypothesis is
    wrong rather than the fix being pointless.

    Also drains stdout: the pipe is what the failure paths read for
    diagnostics, and leaving it unread on the success path is the
    second half of the same leak.
    """
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node_npm_available() -> bool:
    return shutil.which("node") is not None and shutil.which("npm") is not None


def _call_with_hard_timeout(fn, timeout_s: float, *args, **kwargs):
    """Runs fn(*args, **kwargs) in a worker thread with a real deadline,
    independent of any timeout= the function itself defaults to.

    Needed because run_eval_client.create_session/send_message call
    http_post() with a 300s DEFAULT timeout (reasonable for real slow
    model inference in production, not something this test should
    change) -- without this wrapper, the exact hang this test exists
    to catch (see module docstring's KNOWN ENVIRONMENT LIMITATION)
    would make the test itself hang for 5 minutes per call instead of
    failing fast and loud.

    Raises TimeoutError if fn doesn't return within timeout_s. The
    worker thread is daemonized and NOT joined on timeout -- if the
    underlying urllib call is truly stuck, the thread leaks until
    process exit rather than blocking test teardown further.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    finally:
        # wait=False is deliberate: if fn is genuinely stuck (the exact
        # hang this test exists to catch), waiting here would silently
        # re-introduce the multi-minute block this wrapper exists to
        # prevent. The worker thread leaks until process exit instead.
        pool.shutdown(wait=False)


@unittest.skipUnless(_node_npm_available(), "node/npm not on PATH -- cannot run real e2e")
class RunEvalClientE2ETests(unittest.TestCase):
    """Each test method installs+starts its own isolated opencode
    instance and mock backend rather than sharing class-level state --
    slower, but avoids one test's server state leaking into another's,
    which matters more here than raw speed given how much can silently
    go wrong across a real subprocess boundary.
    """

    def _start_mock_backend(self, mode: str, reply_text: str):
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
        import mock_openai_backend  # noqa: E402 (path insert must come first)

        port = _free_port()
        srv, handler_cls = mock_openai_backend.make_server(port, mode, reply_text)
        import threading
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        return port, handler_cls

    def _start_opencode_serve(self, mock_port: int) -> tuple[subprocess.Popen, int, Path]:
        scratch = Path(tempfile.mkdtemp(prefix="opencode-e2e-"))
        home_dir = scratch / "home"
        home_dir.mkdir()
        config_path = scratch / "mock_config.json"
        config_path.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "model": "mock/mock-model",
            "permission": {"edit": "deny", "bash": "deny"},
            "provider": {
                "mock": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Mock",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{mock_port}/v1",
                        "apiKey": "mock",
                    },
                    "models": {"mock-model": {}},
                }
            },
        }))

        install_dir = scratch / "opencode-install"
        install_dir.mkdir()
        subprocess.run(
            ["npm", "install", f"opencode-ai@{OPENCODE_VERSION}"],
            cwd=install_dir, check=True, capture_output=True, timeout=120,
        )
        opencode_bin = install_dir / "node_modules" / ".bin" / "opencode"
        self.assertTrue(opencode_bin.exists(), f"opencode binary not found at {opencode_bin}")

        serve_port = _free_port()
        env = {**os.environ, "HOME": str(home_dir), "OPENCODE_CONFIG": str(config_path)}
        proc = subprocess.Popen(
            [str(opencode_bin), "serve", "--port", str(serve_port), "--hostname", "127.0.0.1"],
            cwd=install_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(_kill_and_reap, proc)

        deadline = time.time() + SERVER_STARTUP_TIMEOUT_S
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", serve_port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            self.fail(f"opencode serve never opened its port. Output:\n{out}")

        # The port being open is not the server being ready -- see
        # READY_ATTEMPT_TIMEOUT_S above. Without this the caller races a
        # window in which its request is silently wedged forever.
        ready_s, attempts = _wait_until_serving(f"http://127.0.0.1:{serve_port}")
        if ready_s is None:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            self.fail(
                f"opencode serve opened its port but never answered an HTTP "
                f"request within {READY_DEADLINE_S}s ({attempts} attempts). "
                f"Output:\n{out}"
            )
        print(f"[e2e] server ready in {ready_s}s after {attempts} attempt(s)", flush=True)

        return proc, serve_port, scratch

    def test_sse_response_matches_documented_schema(self):
        """The core claim this file exists to verify: a real opencode
        serve instance, talking to a real (if minimal) OpenAI-compatible
        SSE backend, produces a response that extract_reply() parses
        correctly via the top-level 'parts'/type=='text' path."""
        mock_port, handler_cls = self._start_mock_backend("sse", "Hello from the mock backend")
        proc, serve_port, scratch = self._start_opencode_serve(mock_port)

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import run_eval_client as rec

        base_url = f"http://127.0.0.1:{serve_port}"
        try:
            session_id = _call_with_hard_timeout(
                rec.create_session, SESSION_REQUEST_TIMEOUT_S, base_url
            )
        except (RuntimeError, concurrent.futures.TimeoutError) as e:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            self.fail(
                f"create_session failed or hung (waited {SESSION_REQUEST_TIMEOUT_S}s): {e}\n"
                f"opencode serve output:\n{out}\n"
                "See module docstring's KNOWN ENVIRONMENT LIMITATION -- "
                "this may be a blocked outbound network call from "
                "opencode itself, not a bug in this test or the harness."
            )

        resp = _call_with_hard_timeout(
            rec.send_message, SESSION_REQUEST_TIMEOUT_S,
            base_url, session_id, "mock", "mock-model", "mock-probe-marker: hello"
        )
        text, tools = rec.extract_reply(resp)

        self.assertIn(
            "Hello from the mock backend", text,
            f"extract_reply() did not find the expected text. Raw response:\n"
            f"{json.dumps(resp, indent=2)}",
        )
        self.assertEqual(tools, [], "no tool calls expected from this mock")

        # TOKEN ACCOUNTING, previously never exercised. The mock did not
        # answer opencode's own `stream_options` request with a usage
        # chunk, so Session.getUsage() fell back to an empty Usage and
        # every run here recorded zeros -- which looked exactly like a
        # correct result. Asserting non-zero is what makes the mock's
        # conformance checkable rather than assumed.
        tokens = resp.get("info", {}).get("tokens") or {}
        self.assertTrue(
            tokens.get("input", 0) > 0 and tokens.get("output", 0) > 0,
            f"usage did not reach the message -- either the mock stopped sending the "
            f"usage chunk or opencode stopped reading it. tokens={tokens}",
        )

    def test_flat_json_backend_is_caught_as_a_provider_fault(self):
        """A backend that ignores stream:true is detected by the HARNESS,
        not left to time out as a model failure.

        This replaces an assertion that opencode returns an empty reply
        in this case. That WAS true through 1.18.20 and is no longer:
        1.18.21 added "unknown" to the loop-exit exclusion list in
        session/prompt.ts, deliberately, so a provider that reports no
        finish reason can no longer end a turn silently -- which is the
        false-pass shape this whole project exists to catch, closed
        upstream. The old assertion pinned the pre-fix bug.

        What is left is a harness problem: opencode keeps prompting, one
        assistant message per provider call, and without detection the
        tier ends as a 300s timeout scored as a model result while the
        provider is charged for every call. Thresholds are lowered here
        so the test costs seconds rather than a minute.
        """
        mock_port, handler_cls = self._start_mock_backend("flat", "should not appear")
        proc, serve_port, scratch = self._start_opencode_serve(mock_port)

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import importlib
        import run_eval_client as rec
        importlib.reload(rec)  # avoid stale module state across test methods
        rec.UNPRODUCTIVE_LOOP_MESSAGES = 5
        rec.UNPRODUCTIVE_CHECK_INTERVAL_S = 2

        base_url = f"http://127.0.0.1:{serve_port}"
        session_id = _call_with_hard_timeout(
            rec.create_session, SESSION_REQUEST_TIMEOUT_S, base_url
        )

        result, fault, events = _call_with_hard_timeout(
            rec.quota_aware_send_message, SESSION_REQUEST_TIMEOUT_S,
            base_url, session_id, "mock", "mock-model", "mock-probe-marker: hello"
        )

        self.assertIsNone(result, "a non-conforming backend must not yield a usable reply")
        self.assertIsNotNone(
            fault,
            "the harness did not detect the loop -- it would have run to the "
            "tier timeout and been scored as a model failure",
        )
        self.assertEqual(fault["kind"], "unproductive_loop")
        self.assertIn("finish=unknown", fault["message"])
        self.assertTrue(
            any(e.get("type") == "unproductive_loop" for e in events),
            f"the tier record must carry the evidence, got: {events}",
        )


if __name__ == "__main__":
    unittest.main()
