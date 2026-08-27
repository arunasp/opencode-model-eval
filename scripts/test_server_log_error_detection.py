#!/usr/bin/env python3
"""Regression tests for the server-log error detection added to
quota_aware_send_message()/run_category(), including the OpenAI
documented error-code classification
(https://developers.openai.com/api/docs/guides/error-codes) that
decides which detected errors are safe to bail out on early versus
which ones opencode's own internal retry (session/retry.ts, confirmed
via its real source) legitimately still has a chance to recover from.

Direct request: run_eval_client.py already has full access to
opencode's own server log (used for the end-of-run capture and the
interrupt handler) -- it should check it DURING the wait for a
response too, not just after. Confirmed live: a real "exceeds the
available context size" failure was visible in the server log almost
instantly, while the client sat blind for the full client-side timeout
waiting on a socket read that opencode's own internal compaction-retry
loop was never going to satisfy.

Second direct request: be aware of the documented OpenAI-style error
codes while still letting opencode's own internal retry keep going
where it legitimately applies. Confirmed via opencode's REAL source
(retry.ts's retryable() function) exactly which classes that is:
ContextOverflowError is explicitly, permanently excluded from retry;
any 5xx status is ALWAYS retried internally ("5xx errors are transient
server failures and should always be retried" -- opencode's own
comment, verbatim); 429/rate-limit classes are also retried internally
(surfaced via the existing _QuotaExhausted/status-polling mechanism);
401/403 fall through to not retried. Bailing out early on a 429 or 5xx
would preempt a retry opencode was legitimately still going to attempt
-- exactly what this classification exists to prevent.

Usage:
    python3 scripts/test_server_log_error_detection.py
"""
import json
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


class ClassifyLogErrorTests(unittest.TestCase):
    """Direct request: be aware of the documented OpenAI-style error
    codes (https://developers.openai.com/api/docs/guides/error-codes)
    while still letting opencode's own internal retry keep going where
    it legitimately applies. Confirmed via opencode's REAL source
    (packages/opencode/src/session/retry.ts's retryable()) exactly
    which classes that is -- these tests encode that confirmed rule as
    a permanent regression guard, not just a one-off verification.
    """

    @staticmethod
    def _make_line(code, message, error_type=None):
        body = {"error": {"code": code, "message": message}}
        if error_type:
            body["error"]["type"] = error_type
        return f'timestamp=x level=ERROR message="stream error" error.error="AI_APICallError: {json.dumps(body)}"'

    def test_context_overflow_bails_out(self):
        # Matches the real live-observed shape exactly. Confirmed via
        # opencode's own source: ContextOverflowError is explicitly,
        # permanently excluded from retry.
        line = self._make_line(400, "request (5602 tokens) exceeds the available context size (4096 tokens)",
                               "exceed_context_size_error")
        result = r._classify_log_error(line)
        self.assertIsNotNone(result)
        self.assertIn("context overflow", result)

    def test_401_bails_out(self):
        result = r._classify_log_error(self._make_line(401, "Invalid Authentication"))
        self.assertIsNotNone(result)
        self.assertIn("401", result)

    def test_403_bails_out(self):
        result = r._classify_log_error(self._make_line(403, "region not supported"))
        self.assertIsNotNone(result)
        self.assertIn("403", result)

    def test_429_does_not_bail_out(self):
        # Confirmed via source: opencode's own retry.ts retries this
        # internally (FreeUsageLimitError/GoUsageLimitError, generic
        # rate-limit text matching) -- surfaced via the existing
        # _QuotaExhausted/status-polling mechanism instead. Bailing out
        # here would preempt a retry opencode was legitimately still
        # going to attempt.
        result = r._classify_log_error(self._make_line(429, "rate limit reached"))
        self.assertIsNone(result)

    def test_500_does_not_bail_out(self):
        # Confirmed via source: "5xx errors are transient server
        # failures and should always be retried" -- opencode's own
        # comment, verbatim.
        result = r._classify_log_error(self._make_line(500, "internal server error"))
        self.assertIsNone(result)

    def test_503_does_not_bail_out(self):
        result = r._classify_log_error(self._make_line(503, "engine overloaded"))
        self.assertIsNone(result)

    def test_unclassified_error_falls_back_to_fail_fast(self):
        line = 'timestamp=x level=ERROR message="some totally unrecognized failure with no code field at all"'
        result = r._classify_log_error(line)
        self.assertIsNotNone(result)
        self.assertIn("unclassified", result)

    def test_real_too_many_requests_text_does_not_bail_out(self):
        # Verbatim from a real NVIDIA log (repeated for hours across
        # multiple sessions) -- no JSON code field at all, just plain
        # text. Confirmed via source: retry.ts's own
        # lower.includes("too many requests") check is what actually
        # retries this -- a code-only regex would have missed it
        # entirely and incorrectly bailed out on something opencode
        # was legitimately retrying.
        line = ('timestamp=2026-07-27T23:43:27.483Z level=ERROR run=c77ca6d5 message="stream error" '
                'providerID=nvidia-ds4pro modelID=deepseek-ai/deepseek-v4-pro '
                'session.id=ses_05a6c7df2ffewJEY35W37ogPTQ small=false '
                'agent=axiom-backend-configurator mode=subagent '
                'error.error="AI_APICallError: Too Many Requests"')
        result = r._classify_log_error(line)
        self.assertIsNone(result)

    def test_real_timeouterror_bails_out_with_precise_classification(self):
        # Verbatim from a real NVIDIA log. Confirmed via source: a bare
        # TimeoutError matches none of retry.ts's retryable classes --
        # opencode has already given up on it internally, so this
        # should be a precise, named bailout, not generic
        # "unclassified".
        line = ('timestamp=2026-07-28T07:22:27.626Z level=ERROR run=cebb9966 message="stream error" '
                'providerID=nvidia-ds4pro modelID=deepseek-ai/deepseek-v4-pro '
                'session.id=ses_058a97c14ffeQtMRKDEJd671eo small=false agent=build mode=primary '
                'error.error="TimeoutError: The operation timed out."')
        result = r._classify_log_error(line)
        self.assertIsNotNone(result)
        self.assertIn("provider-side timeout", result)

    def test_json_exhausted_code_does_not_bail_out(self):
        line = f'level=ERROR error.error="{json.dumps({"code": "resource_exhausted", "message": "quota exhausted"})}"'
        self.assertIsNone(r._classify_log_error(line))

    def test_json_unavailable_code_does_not_bail_out(self):
        payload = json.dumps({"code": "service_unavailable",
                              "message": "temporarily unavailable"})
        line = f'level=ERROR error.error="{payload}"'
        self.assertIsNone(r._classify_log_error(line))

    def test_json_too_many_requests_error_type_does_not_bail_out(self):
        line = f'level=ERROR error.error="{json.dumps({"type": "error", "error": {"type": "too_many_requests"}})}"'
        self.assertIsNone(r._classify_log_error(line))

    def test_full_integration_429_does_not_short_circuit_the_wait(self):
        # End-to-end: a 429 appears in the log mid-poll, but since it's
        # not bailout-worthy, quota_aware_send_message() must just keep
        # polling normally and let the real message complete via the
        # worker thread, exactly as it would have before this
        # detection mechanism existed.
        poll_count = [0]

        def fake_check_log(session_id):
            poll_count[0] += 1
            if poll_count[0] == 1:
                return json.dumps({"error": {"code": 429, "message": "rate limit reached"}})
            return None

        def fake_get_session_status(base_url, session_id):
            return {"type": "idle"}

        def fake_send_message(base_url, session_id, provider, model_id, text, timeout=300):
            return {"info": {"finish": "stop"}, "parts": [{"type": "text", "text": "ok"}]}

        abort_calls = []

        def fake_abort_session(base_url, session_id):
            abort_calls.append(session_id)

        with patch.object(r, "_check_session_log_error", fake_check_log), \
                patch.object(r, "get_session_status", fake_get_session_status), \
                patch.object(r, "send_message", fake_send_message), \
                patch.object(r, "abort_session", fake_abort_session):
            result, quota_info, events = r.quota_aware_send_message(
                "http://server:4096", "ses_1", "local/ollama", "test-model", "hi",
                poll_interval_s=0.05)

        self.assertIsNone(quota_info, "a 429 must not produce a bailout signal")
        self.assertIsNotNone(result)
        self.assertEqual(result["info"]["finish"], "stop")
        self.assertEqual(abort_calls, [], "must not have aborted the session over a 429")


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
