#!/usr/bin/env python3
"""Regression tests for run_category()'s targeted timeout retry.

Real gap this closes: a genuine client-side socket timeout ("timed out
after Ns waiting for a response" -- confirmed this means http_post()'s
own read timeout fired with truly no bytes back, not a parsing bug)
previously went straight to "E" with zero retry. Distinct from quota
exhaustion (already has its own bounded-wait/give-up logic,
untouched here) and deliberately NOT a blanket retry-everything: a
deterministic failure like ContextOverflowError would just fail
identically on retry, wasting another full timeout window for nothing.

Usage:
    python3 scripts/test_run_category_timeout_retry.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval_client as r  # noqa: E402

TIMEOUT_MSG = ("POST /session/x/message timed out after 300s waiting for a "
               "response from http://server:4096/session/x/message")
CONTEXT_OVERFLOW_MSG = ("opencode returned an error response: "
                         "ContextOverflowError: Session too large to compact")


def _make_category():
    return {
        "id": "test_category",
        "description": "test",
        "tiers": [{"tier": 1, "source": "test", "prompt": "probe", "pass_criteria": {}}],
    }


class TimeoutRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.call_log = []

    def _fake_create_session(self, base_url):
        self.call_log.append("create_session")
        return f"ses_{len(self.call_log)}"

    def _fake_abort_session(self, base_url, session_id):
        self.call_log.append(f"abort:{session_id}")

    def test_succeeds_after_one_retry(self):
        def fake_send(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            if len([c for c in self.call_log if c.startswith("send:")]) == 1:
                raise RuntimeError(TIMEOUT_MSG)
            return {"info": {"finish": "stop"}, "parts": [{"type": "text", "text": "ok"}]}, None, []

        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send), \
             patch.object(r, "abort_session", self._fake_abort_session), \
             patch.object(r, "scan_transcript", lambda p: {"category_counts": {}}), \
             patch.object(r, "check_pass", lambda scan, criteria: (True, "pass_criteria satisfied")):
            result = r.run_category(_make_category(), "http://server:4096", "local/ollama",
                                     "test-model", "setup", self.tmpdir)

        self.assertTrue(result["tiers"][0]["passed"])
        self.assertEqual(result["ceiling"], 1)
        create_calls = [c for c in self.call_log if c == "create_session"]
        self.assertEqual(len(create_calls), 2, "should have created a fresh session for the retry")

    def test_exhausts_retry_and_marks_error(self):
        def fake_send_always_timeout(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            raise RuntimeError(TIMEOUT_MSG)

        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send_always_timeout), \
             patch.object(r, "abort_session", self._fake_abort_session):
            result = r.run_category(_make_category(), "http://server:4096", "local/ollama",
                                     "test-model", "setup", self.tmpdir)

        self.assertFalse(result["tiers"][0]["passed"])
        self.assertIn("HTTP/request error", result["tiers"][0]["reason"])
        create_calls = [c for c in self.call_log if c == "create_session"]
        self.assertEqual(len(create_calls), 1 + r.TIER_TIMEOUT_RETRY_LIMIT,
                          "should try exactly 1 + TIER_TIMEOUT_RETRY_LIMIT times, no more")

    def test_non_timeout_error_does_not_retry(self):
        def fake_send_context_overflow(base_url, session_id, provider, model_id, text, **kwargs):
            self.call_log.append(f"send:{text[:10]}")
            raise RuntimeError(CONTEXT_OVERFLOW_MSG)

        with patch.object(r, "create_session", self._fake_create_session), \
             patch.object(r, "quota_aware_send_message", fake_send_context_overflow), \
             patch.object(r, "abort_session", self._fake_abort_session):
            result = r.run_category(_make_category(), "http://server:4096", "local/ollama",
                                     "test-model", "setup", self.tmpdir)

        self.assertFalse(result["tiers"][0]["passed"])
        create_calls = [c for c in self.call_log if c == "create_session"]
        self.assertEqual(len(create_calls), 1, "a deterministic (non-timeout) error must not retry")


if __name__ == "__main__":
    unittest.main()
