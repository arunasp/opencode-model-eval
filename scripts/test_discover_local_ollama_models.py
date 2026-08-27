#!/usr/bin/env python3
"""Regression tests for discover_local_ollama_models.py.

Stdlib-only (unittest + http.server), per CODEGEN.md's Python
conventions. Runs a real local HTTP server rather than mocking
urllib -- exercises the actual request/response path, not a stand-in
for it.

Usage:
    python3 scripts/test_discover_local_ollama_models.py
"""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_local_ollama_models as discover  # noqa: E402


FAKE_MODELS = [
    "gemma4:31b",
    "nemotron-3-nano:30b",
    "qwen3-coder:30b",
    "qwen3-coder-fixed:30b",
    "qwen2.5-coder:7b",
]


class _FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib method name)
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": n} for n in FAKE_MODELS]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence per-request logging in test output
        pass


class DiscoverLocalOllamaModelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)
        # shutdown() ends the serve_forever loop; the listening socket
        # stays open until server_close(). Omitting it leaks the socket
        # and Python reports ResourceWarning: unclosed socket. The same
        # omission existed independently in test_run_eval_client_e2e.py,
        # which is why the test stage now fails on any ResourceWarning
        # rather than leaving it to be noticed.
        cls.server.server_close()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_config_path = Path(self.tmpdir.name) / "base.json"
        self.base_config_path.write_text(
            json.dumps(
                {
                    "provider": {
                        "local/ollama": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": "{env:OPENCODE_OLLAMA_BASE_URL}"},
                            # Deliberately stale/wrong single entry -- proves
                            # a successful discovery actually overwrites it,
                            # not just leaves it alone by coincidence.
                            "models": {"gemma4:31b": {}},
                        }
                    }
                }
            )
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_successful_discovery_merges_into_static_model_list(self):
        output_path = Path(self.tmpdir.name) / "out.json"
        rc = discover.main(
            [
                "--base-config", str(self.base_config_path),
                "--ollama-tags-url", f"http://127.0.0.1:{self.port}/api/tags",
                "--output", str(output_path),
                "--provider-key", "local/ollama",
                "--timeout", "3",
            ]
        )
        self.assertEqual(rc, 0)
        result = json.loads(output_path.read_text())
        discovered = sorted(result["provider"]["local/ollama"]["models"].keys())
        self.assertEqual(discovered, sorted(FAKE_MODELS))

    def test_declared_model_not_discovered_survives_the_merge(self):
        # The real bug this guards: a model declared in the base config
        # but not currently reported by Ollama's live /api/tags (not
        # pulled yet on this run, or a transient discovery gap) must not
        # silently vanish -- confirmed live this session with
        # NitrAI/VibeThinker-3B:latest disappearing from a host-side
        # picker because an earlier version of merge_models() did a full
        # replacement instead of a union.
        base_config_path = Path(self.tmpdir.name) / "base_with_extra.json"
        base_config_path.write_text(
            json.dumps({
                "provider": {
                    "local/ollama": {
                        "models": {
                            "gemma4:31b": {},
                            "declared-but-not-installed:latest": {},
                        }
                    }
                }
            })
        )
        output_path = Path(self.tmpdir.name) / "out.json"
        rc = discover.main(
            [
                "--base-config", str(base_config_path),
                "--ollama-tags-url", f"http://127.0.0.1:{self.port}/api/tags",
                "--output", str(output_path),
                "--provider-key", "local/ollama",
                "--timeout", "3",
            ]
        )
        self.assertEqual(rc, 0)
        result = json.loads(output_path.read_text())
        discovered = set(result["provider"]["local/ollama"]["models"].keys())
        self.assertEqual(discovered, set(FAKE_MODELS) | {"declared-but-not-installed:latest"})

    def test_unreachable_ollama_is_not_fatal_and_leaves_static_list_unchanged(self):
        output_path = Path(self.tmpdir.name) / "out.json"
        # Port 1 is a real reserved port nothing will ever bind to in a
        # test sandbox -- connection refused is the realistic failure
        # mode, not an artificial mock.
        rc = discover.main(
            [
                "--base-config", str(self.base_config_path),
                "--ollama-tags-url", "http://127.0.0.1:1/api/tags",
                "--output", str(output_path),
                "--provider-key", "local/ollama",
                "--timeout", "1",
            ]
        )
        self.assertEqual(rc, 0, "unreachable Ollama must not be treated as fatal")
        result = json.loads(output_path.read_text())
        discovered = list(result["provider"]["local/ollama"]["models"].keys())
        self.assertEqual(
            discovered,
            ["gemma4:31b"],
            "fallback must preserve the original static config, not partially merge",
        )

    def test_missing_provider_key_raises(self):
        output_path = Path(self.tmpdir.name) / "out.json"
        with self.assertRaises(KeyError):
            discover.merge_models(
                json.loads(self.base_config_path.read_text()),
                "nonexistent/provider",
                FAKE_MODELS,
            )


if __name__ == "__main__":
    unittest.main()
