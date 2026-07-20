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

Requires: node + npm on PATH, and network access to the npm registry
(registry.npmjs.org) to install opencode-ai on first run, PLUS
whatever network access opencode's own `serve`/session-creation path
needs internally (see KNOWN ENVIRONMENT LIMITATION below).

KNOWN ENVIRONMENT LIMITATION, found while building this test: in a
network-restricted sandbox (only specific domains allowlisted, e.g.
npmjs.org/registry.npmjs.org but NOT opencode.ai), `POST /session`
hung indefinitely and the mock backend's request log stayed completely
empty -- meaning opencode never even reached the configured provider.
This points at an outbound call opencode itself makes during session
creation (telemetry/update-check, unconfirmed which) to a domain
outside a restricted allowlist, with no fast-fail/offline mode
observed. Isolated by testing the mock backend directly (bypassing
opencode entirely) -- confirmed correct on its own: valid /v1/models
response, valid SSE chunk stream. The failure is entirely within
opencode's own startup path, not this test's mock or harness code.

If this test hangs/times out in your environment: check whether
opencode.ai (or another opencode-controlled domain) needs to be
network-reachable, or run with full/unrestricted egress. This test
uses a hard subprocess timeout specifically so a repeat of that
failure mode fails loudly and fast instead of hanging your CI.

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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENCODE_VERSION = "1.18.3"  # matches the version the original empirical
                              # claim in run_eval_client.py's docstring
                              # was made against
SERVER_STARTUP_TIMEOUT_S = 15
SESSION_REQUEST_TIMEOUT_S = 20  # generous, but bounded -- see module
                                  # docstring's KNOWN ENVIRONMENT LIMITATION


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
        self.addCleanup(proc.kill)

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

    def test_flat_json_response_reproduces_documented_gotcha(self):
        """The negative case documented in the module docstring: if the
        backend ignores stream:true and returns flat synchronous JSON,
        opencode is documented to silently produce a response with NO
        text part (not an error). Confirms that's still true, rather
        than asserting it from memory of a prior session."""
        mock_port, handler_cls = self._start_mock_backend("flat", "should not appear")
        proc, serve_port, scratch = self._start_opencode_serve(mock_port)

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import importlib
        import run_eval_client as rec
        importlib.reload(rec)  # avoid stale module state across test methods

        base_url = f"http://127.0.0.1:{serve_port}"
        try:
            session_id = _call_with_hard_timeout(
                rec.create_session, SESSION_REQUEST_TIMEOUT_S, base_url
            )
        except (RuntimeError, concurrent.futures.TimeoutError) as e:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
            self.fail(
                f"create_session failed or hung (waited {SESSION_REQUEST_TIMEOUT_S}s): "
                f"{e}\nopencode serve output:\n{out}"
            )

        resp = _call_with_hard_timeout(
            rec.send_message, SESSION_REQUEST_TIMEOUT_S,
            base_url, session_id, "mock", "mock-model", "mock-probe-marker: hello"
        )
        text, tools = rec.extract_reply(resp)

        self.assertEqual(
            text, "",
            "documented gotcha did not reproduce -- either opencode's "
            "behavior changed, or the flat-JSON mock mode is no longer "
            "accurate. Update the docstring in run_eval_client.py if "
            "this genuinely changed upstream, don't just loosen this "
            f"assertion. Raw response:\n{json.dumps(resp, indent=2)}",
        )


if __name__ == "__main__":
    unittest.main()
