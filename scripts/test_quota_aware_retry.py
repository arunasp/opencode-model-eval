#!/usr/bin/env python3
"""Regression tests for quota_aware_send_message() and run_category()'s
Q (quota-exhausted) handling.

Stdlib-only (unittest + threading), per CODEGEN.md's Python
conventions. Uses real threading.Event-based coordination to control
timing deterministically -- no sleep-and-hope racing.

Usage:
    python3 scripts/test_quota_aware_retry.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval_client as rec  # noqa: E402


class QuotaAwareSendMessageTests(unittest.TestCase):
    def setUp(self):
        # Every test replaces these module-level names; restore the
        # real ones afterward so tests don't leak into each other or
        # into other test files run in the same process.
        self._orig_send_message = rec.send_message
        self._orig_get_session_status = rec.get_session_status
        self._orig_abort_session = rec.abort_session
        self._orig_create_session = rec.create_session

    def tearDown(self):
        rec.send_message = self._orig_send_message
        rec.get_session_status = self._orig_get_session_status
        rec.abort_session = self._orig_abort_session
        rec.create_session = self._orig_create_session

    def test_normal_completion_returns_no_quota_info(self):
        rec.send_message = lambda base_url, sid, p, m, t: {
            "parts": [{"type": "text", "text": "a normal reply"}]
        }
        rec.get_session_status = lambda base_url, sid: {"type": "idle"}

        result, quota_info, events = rec.quota_aware_send_message(
            "http://fake", "sess1", "opencode", "model1", "hello", poll_interval_s=0.05
        )
        self.assertIsNone(quota_info)
        self.assertEqual(result, {"parts": [{"type": "text", "text": "a normal reply"}]})

    def test_quota_exhaustion_aborts_and_reports_cleanly(self):
        release_worker = threading.Event()

        def blocking_send_message(base_url, sid, p, m, t):
            release_worker.wait(timeout=10)
            return {"parts": [{"type": "text", "text": "should never be used"}]}

        rec.send_message = blocking_send_message

        abort_calls = []

        def fake_abort(base_url, sid):
            abort_calls.append(sid)
            release_worker.set()
            return True

        rec.abort_session = fake_abort

        far_future_ms = (time.time() + 3600) * 1000
        rec.get_session_status = lambda base_url, sid: {
            "type": "retry",
            "attempt": 3,
            "message": "Rate Limited",
            "action": {"reason": "account_rate_limit", "provider": "nvidia"},
            "next": far_future_ms,
        }

        result, quota_info, events = rec.quota_aware_send_message(
            "http://fake", "sess2", "opencode", "model1", "hello",
            quota_wait_threshold_s=5, poll_interval_s=0.05,
        )

        self.assertIsNone(result)
        self.assertIsNotNone(quota_info)
        self.assertEqual(quota_info["reason"], "account_rate_limit")
        self.assertGreater(quota_info["wait_seconds"], 3000)
        self.assertEqual(abort_calls, ["sess2"], "abort should fire exactly once")
        self.assertGreaterEqual(len(events), 1)

    def test_short_retry_under_threshold_waits_patiently_no_abort(self):
        release_worker = threading.Event()

        def delayed_send_message(base_url, sid, p, m, t):
            release_worker.wait(timeout=5)
            return {"parts": [{"type": "text", "text": "succeeded after internal retry"}]}

        rec.send_message = delayed_send_message

        abort_calls = []
        rec.abort_session = lambda base_url, sid: abort_calls.append(sid) or True

        poll_count = [0]

        def short_retry_status(base_url, sid):
            poll_count[0] += 1
            if poll_count[0] >= 3:
                release_worker.set()
            near_future_ms = (time.time() + 0.3) * 1000
            return {
                "type": "retry", "attempt": 1, "message": "brief backoff",
                "action": {"reason": "account_rate_limit", "provider": "opencode"},
                "next": near_future_ms,
            }

        rec.get_session_status = short_retry_status

        result, quota_info, events = rec.quota_aware_send_message(
            "http://fake", "sess3", "opencode", "model1", "hello",
            quota_wait_threshold_s=5, poll_interval_s=0.05,
        )

        self.assertIsNone(quota_info, "short backoff must not be treated as quota exhaustion")
        self.assertEqual(result, {"parts": [{"type": "text", "text": "succeeded after internal retry"}]})
        self.assertEqual(abort_calls, [], "abort must never fire for an under-threshold retry")


class RunCategoryQuotaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._orig_send_message = rec.send_message
        self._orig_get_session_status = rec.get_session_status
        self._orig_abort_session = rec.abort_session
        self._orig_create_session = rec.create_session
        self._orig_scan_transcript = rec.scan_transcript
        self._orig_qasm = rec.quota_aware_send_message
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        rec.send_message = self._orig_send_message
        rec.get_session_status = self._orig_get_session_status
        rec.abort_session = self._orig_abort_session
        rec.create_session = self._orig_create_session
        rec.scan_transcript = self._orig_scan_transcript
        rec.quota_aware_send_message = self._orig_qasm

    def test_run_category_marks_q_on_quota_exhaustion(self):
        rec.create_session = lambda base_url: "fake-session"

        release_worker = threading.Event()
        rec.send_message = lambda base_url, sid, p, m, t: (
            release_worker.wait(timeout=10) or {"parts": []}
        )

        abort_calls = []

        def fake_abort(base_url, sid):
            abort_calls.append(sid)
            release_worker.set()
            return True

        rec.abort_session = fake_abort

        far_future_ms = (time.time() + 3600) * 1000
        rec.get_session_status = lambda base_url, sid: {
            "type": "retry", "attempt": 2,
            "message": "Free usage exceeded, subscribe to Go",
            "action": {"reason": "free_tier_limit", "provider": "opencode"},
            "next": far_future_ms,
        }

        # run_category calls the bare `quota_aware_send_message` name --
        # wrap it with our test's tight threshold rather than waiting
        # out the real 50-minute default.
        orig = rec.quota_aware_send_message

        def wrapped(base_url, sid, p, m, t, **kwargs):
            return orig(base_url, sid, p, m, t, quota_wait_threshold_s=5, poll_interval_s=0.05)

        rec.quota_aware_send_message = wrapped

        category = {
            "id": "quota_test_category",
            "description": "Tests quota-exhaustion handling end-to-end",
            "tiers": [{"tier": 1, "source": "validated", "prompt": "p1", "pass_criteria": {}}],
        }
        result = rec.run_category(category, "http://fake", "opencode", "deepseek-v4-flash-free",
                                   "setup", self.tmpdir / "cat")

        self.assertEqual(result["progress_dots"], "Q")
        self.assertEqual(result["ceiling"], 0)
        self.assertTrue(result["tiers"][0]["reason"].startswith("quota/rate-limit exhausted"))
        self.assertGreater(result["tiers"][0]["quota_wait_seconds"], 3000)
        self.assertIn("status_events", result["tiers"][0])
        self.assertEqual(abort_calls, ["fake-session"])

    def test_run_category_regressions_still_pass_after_quota_addition(self):
        rec.create_session = lambda base_url: "fake-session"
        rec.scan_transcript = lambda p: {"category_counts": {}, "total_findings": 0}

        responses = []

        def fake_send_message(base_url, sid, p, m, t):
            behavior = responses.pop(0)
            if behavior == "error":
                raise RuntimeError("simulated network error")
            return {"parts": [{"type": "text", "text": behavior}]}

        rec.send_message = fake_send_message
        rec.get_session_status = lambda base_url, sid: {"type": "idle"}

        # All-pass
        cat_pass = {"id": "regress_pass", "description": "d", "tiers": [
            {"tier": 1, "source": "validated", "prompt": "p1", "pass_criteria": {}},
            {"tier": 2, "source": "validated", "prompt": "p2", "pass_criteria": {}},
        ]}
        responses.clear()
        responses += ["s", "r1", "s", "r2"]
        r = rec.run_category(cat_pass, "http://fake", "opencode", "m", "setup", self.tmpdir / "p")
        self.assertEqual(r["progress_dots"], "..")

        # Fail at tier 2, tier 3 never runs
        cat_fail = {"id": "regress_fail", "description": "d", "tiers": [
            {"tier": 1, "source": "validated", "prompt": "p1", "pass_criteria": {}},
            {"tier": 2, "source": "new", "prompt": "p2", "pass_criteria": {"must_have_categories": ["x"]}},
            {"tier": 3, "source": "new", "prompt": "p3", "pass_criteria": {}},
        ]}
        responses.clear()
        responses += ["s", "r1", "s", "r2"]
        r = rec.run_category(cat_fail, "http://fake", "opencode", "m", "setup", self.tmpdir / "f")
        self.assertEqual(r["progress_dots"], ".F")
        self.assertEqual(len(r["tiers"]), 2)

        # Generic error
        cat_err = {"id": "regress_err", "description": "d", "tiers": [
            {"tier": 1, "source": "validated", "prompt": "p1", "pass_criteria": {}},
        ]}
        responses.clear()
        responses += ["error"]
        r = rec.run_category(cat_err, "http://fake", "opencode", "m", "setup", self.tmpdir / "e")
        self.assertEqual(r["progress_dots"], "E")


if __name__ == "__main__":
    unittest.main()
