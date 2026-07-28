#!/usr/bin/env python3
"""Regression tests for the server-log error detection added to
quota_aware_send_message()/run_category().

Direct request: run_eval_client.py already has full access to
opencode's own server log (used for the end-of-run capture and the
interrupt handler) -- it should check it DURING the wait for a
response too, not just after. Confirmed live: a real "exceeds the
available context size" failure was visible in the server log almost
instantly, while the client sat blind for the full client-side timeout
waiting on a socket read that opencode's own internal compaction-retry
loop was never going to satisfy.

Usage:
    python3 scripts/test_server_log_error_detection.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval_client as r  # noqa: E402


class CheckSessionLogErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.fake_log = self.tmpdir / "opencode.log"
        self._orig_log_path = r.OPENCODE_LOG_PATH

    def tearDown(self):
        r.OPENCODE_LOG_PATH = self._orig_log_path

    def test_matching_session_error_found(self):
        self.fake_log.write_text(
            'timestamp=2026-07-28T08:05:59.498Z level=INFO run=x message=stream session.id=ses_TEST123\n'
            'timestamp=2026-07-28T08:05:59.920Z level=ERROR run=x message="stream error" '
            'session.id=ses_TEST123 error.error="exceeds the available context size"\n'
        )
        r.OPENCODE_LOG_PATH = self.fake_log
        result = r._check_session_log_error("ses_TEST123")
        self.assertIsNotNone(result)
        self.assertIn("level=ERROR", result)
        self.assertIn("ses_TEST123", result)

    def test_unrelated_session_not_matched(self):
        self.fake_log.write_text(
            'timestamp=2026-07-28T08:05:59.920Z level=ERROR session.id=ses_TEST123 error="x"\n'
        )
        r.OPENCODE_LOG_PATH = self.fake_log
        result = r._check_session_log_error("ses_OTHER456")
        self.assertIsNone(result)

    def test_missing_log_file_handled_gracefully(self):
        r.OPENCODE_LOG_PATH = self.tmpdir / "does_not_exist.log"
        result = r._check_session_log_error("ses_TEST123")
        self.assertIsNone(result)

    def test_info_only_lines_do_not_match(self):
        self.fake_log.write_text(
            'timestamp=2026-07-28T08:05:59.498Z level=INFO session.id=ses_TEST123 message=stream\n'
        )
        r.OPENCODE_LOG_PATH = self.fake_log
        result = r._check_session_log_error("ses_TEST123")
        self.assertIsNone(result)


class RunCategoryServerLogErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.call_log = []

    def _fake_create_session(self, base_url):
        self.call_log.append("create_session")
        return f"ses_{len(self.call_log)}"

    def _fake_abort_session(self, base_url, session_id):
        self.call_log.append(f"abort:{session_id}")

    def test_server_log_error_marks_tier_error_with_precise_message(self):
        def fake_send(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            return None, {
                "kind": "server_log_error",
                "message": 'level=ERROR ... exceeds the available context size (4096 tokens)',
            }, []

        category = {"id": "c1", "description": "test",
                    "tiers": [{"tier": 1, "source": "test", "prompt": "probe", "pass_criteria": {}}]}
        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send), \
             patch.object(r, "abort_session", self._fake_abort_session):
            result = r.run_category(category, "http://server:4096", "local/ollama",
                                     "test-model", "setup", self.tmpdir)

        self.assertFalse(result["tiers"][0]["passed"])
        self.assertIn("opencode server log error", result["tiers"][0]["reason"])
        self.assertIn("exceeds the available context size", result["tiers"][0]["reason"])
        self.assertEqual(result["progress_dots"], "E")

    def test_server_log_error_does_not_retry(self):
        # Deliberately different from a genuine socket timeout: this
        # failure class is deterministic (confirmed live: a context-size
        # mismatch fails identically every time), so it must NOT go
        # through the timeout-retry logic.
        def fake_send(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            return None, {"kind": "server_log_error", "message": "some server error"}, []

        category = {"id": "c1", "description": "test",
                    "tiers": [{"tier": 1, "source": "test", "prompt": "probe", "pass_criteria": {}}]}
        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send), \
             patch.object(r, "abort_session", self._fake_abort_session):
            r.run_category(category, "http://server:4096", "local/ollama",
                            "test-model", "setup", self.tmpdir)

        create_calls = [c for c in self.call_log if c == "create_session"]
        self.assertEqual(len(create_calls), 1, "a server-log error must not trigger a retry")

    def test_quota_bailout_unaffected_by_kind_field_addition(self):
        # Regression check: adding "kind" to the returned dict must not
        # break the pre-existing quota-exhaustion path.
        def fake_send_quota(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            return None, {"kind": "quota", "reason": "rate_limited",
                          "wait_seconds": 3600, "message": "quota exceeded"}, []

        category = {"id": "c1", "description": "test",
                    "tiers": [{"tier": 1, "source": "test", "prompt": "probe", "pass_criteria": {}}]}
        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send_quota), \
             patch.object(r, "abort_session", self._fake_abort_session):
            result = r.run_category(category, "http://server:4096", "local/ollama",
                                     "test-model", "setup", self.tmpdir)

        self.assertTrue(result["tiers"][0]["reason"].startswith("quota/rate-limit exhausted"))
        self.assertEqual(result["progress_dots"], "Q")


if __name__ == "__main__":
    unittest.main()
