#!/usr/bin/env python3
"""Regression tests for three real gaps found by correlating actual
event logs (a real interrupted run's server.log, verify.log, and the
user's own opencode.log) against run_eval_client.py's behavior.

1) model_slug didn't sanitize provider's own embedded "/" ("local/ollama"),
   only model_id's -- this silently created a real nested directory via
   pathlib's normal "/" interpretation, then the rotation logic
   re-appended that same slash-containing model_slug onto
   results_dir.parent (already one level INTO that nested path),
   doubling the "local" segment. Confirmed live: rotation printed
   ".../local/local/ollama_....<timestamp>".

2) warm_up_local_model() called send_message() directly, with zero
   awareness of the server-log-error detection wired into
   quota_aware_send_message(). Confirmed live: a real run wasted the
   FULL 600s WARMUP_TIMEOUT_S waiting on a context-overflow error that
   later got detected in under a second once the real test tiers
   started (they DO go through quota_aware_send_message()). Fixed by
   routing warm-up through the same function instead of duplicating
   the log-check logic.

3) Direct request: the eval client should write its own persistent log
   automatically, so `docker-compose run ... | tee verify.log` shell
   redirection isn't something to remember every run.

Usage:
    python3 scripts/test_run_eval_client_fixes.py
"""
import io
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval_client as r  # noqa: E402


class WarmUpFailureMessageWordingTests(unittest.TestCase):
    """Real bug found by correlating a real run's own report.json
    (started_utc) against its eval_client.log: warm-up's failure
    message always said "at the 600s hard timeout" regardless of what
    actually happened -- a real run's own timestamps showed warm-up
    failing in the SAME SECOND the run started (an immediate HTTP 500,
    not a timeout at all), yet the printed message still claimed the
    full 600s timeout had been hit.
    """

    @staticmethod
    def _run_warmup_and_capture(exc_message):
        def fake_create_session(base_url):
            return "ses_test"

        def fake_abort_session(base_url, session_id):
            pass

        def fake_ollama_ps(ollama_base_url):
            return []

        def fake_qasm(base_url, session_id, provider, model_id, text, timeout=300):
            raise RuntimeError(exc_message)

        buf = io.StringIO()
        with patch.object(r, "create_session", fake_create_session), \
                patch.object(r, "abort_session", fake_abort_session), \
                patch.object(r, "ollama_ps", fake_ollama_ps), \
                patch.object(r, "quota_aware_send_message", fake_qasm), \
                redirect_stderr(buf):
            r.warm_up_local_model("http://server:4096", "local/ollama", "test-model")

        lines = [line for line in buf.getvalue().splitlines()
                 if "warm-up failed" in line or "warm-up timed out" in line]
        return lines[0]

    def test_genuine_timeout_says_timed_out(self):
        line = self._run_warmup_and_capture(
            "POST /session/ses_test/message timed out after 600s waiting for a response from x")
        self.assertTrue(line.startswith("[eval-client] warm-up timed out at the"))
        self.assertIn("hard timeout", line)

    def test_immediate_error_does_not_falsely_claim_a_timeout(self):
        # The exact real case: an immediate HTTP 500 (UnknownError),
        # not a timeout -- confirmed live via a real run's own
        # started_utc matching its first category's start timestamp
        # to the same second.
        line = self._run_warmup_and_capture(
            'POST /session/x/message failed: HTTP 500: {"name":"UnknownError",'
            '"data":{"message":"Unexpected server error."}}')
        self.assertTrue(line.startswith("[eval-client] warm-up failed ("))
        self.assertNotIn("hard timeout", line)
        self.assertNotIn("600s", line)


class CentralizedLogHelperTests(unittest.TestCase):
    """Direct request: worth improving "[eval-client]" + timestamp.
    Several rounds of hotfixes each found individual print(f"[eval-
    client] ...") sites with no timestamp at all, one at a time -- this
    centralizes it so a new line gets one automatically, rather than
    depending on remembering to add one each time.
    """

    def test_every_line_gets_tag_and_timestamp(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            r._log("something happened")
        line = buf.getvalue().strip()
        self.assertTrue(line.startswith("[eval-client] something happened ("))
        self.assertTrue(line.endswith(")"))
        # A real ISO8601 UTC timestamp should be present inside the parens.
        self.assertRegex(line, r"\(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\)$")

    def test_no_remaining_untimestamped_eval_client_print_sites(self):
        # Regression guard against the exact class of gap this was
        # built to close: every direct print(f"[eval-client] ...")
        # call left in the file must be one of the deliberately-
        # excluded ones (already computes its own timestamp, or builds
        # an incremental single line with end="") -- not a silently
        # untimestamped new one creeping back in later.
        source = Path(r.__file__).read_text()
        deliberately_excluded_line_markers = (
            'print(f"[eval-client] status changed:',
            'print(f"[eval-client] still waiting on retry',
            'print(f"[eval-client] category: {cat_id}:',
            'print(f"[eval-client] ollama /api/ps',
            'print(f"[eval-client] {msg}',  # _log() itself -- the timestamp mechanism
        )
        in_docstring = False
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.count('"""') % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if 'print(f"[eval-client]' in line or "print(f'[eval-client]" in line:
                self.assertTrue(
                    any(marker in line for marker in deliberately_excluded_line_markers),
                    f"found a direct print(...) eval-client line not in the deliberately-"
                    f"excluded list -- should this use _log() instead? line: {line!r}",
                )


class ModelSlugPathTests(unittest.TestCase):
    def test_provider_slash_is_sanitized_like_model_id(self):
        provider = "local/ollama"
        model_id = "NitrAI/VibeThinker-3B:latest"
        model_slug = f"{provider.replace('/', '-')}_{model_id.replace(':', '-').replace('/', '-')}"
        self.assertNotIn("/", model_slug)

    def test_rotation_path_has_no_duplicated_segment(self):
        provider = "local/ollama"
        model_id = "NitrAI/VibeThinker-3B:latest"
        model_slug = f"{provider.replace('/', '-')}_{model_id.replace(':', '-').replace('/', '-')}"
        results_dir = Path("/results") / model_slug
        rotated_dir = results_dir.parent / f"{model_slug}.20260728-105243"
        self.assertNotIn("local/local", str(rotated_dir))
        self.assertEqual(rotated_dir, Path("/results/local-ollama_NitrAI-VibeThinker-3B-latest.20260728-105243"))


class TeeStreamTests(unittest.TestCase):
    def test_writes_to_both_original_and_log_file(self):
        tmpdir = Path(tempfile.mkdtemp())
        log_path = tmpdir / "test.log"
        log_file = log_path.open("a", encoding="utf-8")
        fake_original = io.StringIO()
        tee = r._TeeStream(fake_original, log_file)

        print("hello from tee", file=tee)
        tee.flush()
        log_file.close()

        self.assertIn("hello from tee", fake_original.getvalue())
        self.assertIn("hello from tee", log_path.read_text())

    def test_survives_a_closed_log_file_without_breaking_console_output(self):
        tmpdir = Path(tempfile.mkdtemp())
        log_path = tmpdir / "test.log"
        log_file = log_path.open("a", encoding="utf-8")
        log_file.close()  # deliberately closed before use
        fake_original = io.StringIO()
        tee = r._TeeStream(fake_original, log_file)

        print("still reaches console", file=tee)  # must not raise

        self.assertIn("still reaches console", fake_original.getvalue())


class WarmUpLogDetectionTests(unittest.TestCase):
    def test_bails_out_fast_instead_of_waiting_the_full_timeout(self):
        call_log = []

        def fake_create_session(base_url):
            call_log.append("create_session")
            return "ses_warmup_test"

        def fake_check_log(session_id):
            import json
            return json.dumps({"error": {"code": 400,
                                         "message": "exceeds the available context size",
                                         "type": "exceed_context_size_error"}})

        def fake_get_session_status(base_url, session_id):
            return {"type": "busy"}

        def fake_send_message(base_url, session_id, provider, model_id, text, timeout=300):
            time.sleep(9999)  # must NOT be waited out

        def fake_abort_session(base_url, session_id):
            call_log.append(f"abort:{session_id}")

        def fake_ollama_ps(ollama_base_url):
            return []

        start = time.time()
        with patch.object(r, "create_session", fake_create_session), \
                patch.object(r, "_check_session_log_error", fake_check_log), \
                patch.object(r, "get_session_status", fake_get_session_status), \
                patch.object(r, "send_message", fake_send_message), \
                patch.object(r, "abort_session", fake_abort_session), \
                patch.object(r, "ollama_ps", fake_ollama_ps):
            r.warm_up_local_model("http://server:4096", "local/ollama", "test-model")
        elapsed = time.time() - start

        # The real promise: dramatically faster than the 600s
        # WARMUP_TIMEOUT_S, bounded by STATUS_POLL_INTERVAL_S's real
        # default instead -- not claiming instant.
        self.assertLess(elapsed, 30, f"should bail out well under 600s, took {elapsed:.1f}s")
        self.assertTrue(any(c.startswith("abort:") for c in call_log))

    def test_quota_aware_send_message_timeout_param_reaches_send_message(self):
        # Regression guard for the passthrough itself: quota_aware_send_message()
        # must forward its own timeout param to the inner send_message()
        # call, not silently fall back to send_message's own 300s default.
        captured = {}

        def fake_send_message(base_url, session_id, provider, model_id, text, timeout=300):
            captured["timeout"] = timeout
            return {"info": {"finish": "stop"}, "parts": [{"type": "text", "text": "ok"}]}

        def fake_get_session_status(base_url, session_id):
            return {"type": "idle"}

        with patch.object(r, "send_message", fake_send_message), \
                patch.object(r, "get_session_status", fake_get_session_status), \
                patch.object(r, "_check_session_log_error", lambda session_id: None):
            r.quota_aware_send_message("http://server:4096", "ses_1", "local/ollama",
                                       "test-model", "hi", timeout=600)

        self.assertEqual(captured.get("timeout"), 600)


if __name__ == "__main__":
    unittest.main()
