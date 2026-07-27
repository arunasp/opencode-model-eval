#!/usr/bin/env python3
"""Regression tests for extract_reply()'s finish=="error" detection.

Real bug this guards: a genuine ContextOverflowError response (parts:
[], info.finish: "error") was silently extracted as empty text and
scored a clean PASS by check_pass()/scan_transcript() against an empty
transcript -- confirmed live against VibeThinker-3B (real captured
tier1.raw.json, embedded below verbatim as the fixture).

Usage:
    python3 scripts/test_extract_reply.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval_client import extract_reply  # noqa: E402

# Verbatim from a real captured tier1.raw.json (VibeThinker-3B,
# ContextOverflowError during opencode's own auto-compaction).
REAL_CONTEXT_OVERFLOW_RESPONSE = {
    "info": {
        "role": "assistant",
        "mode": "compaction",
        "agent": "compaction",
        "cost": 0,
        "modelID": "NitrAI/VibeThinker-3B:latest",
        "providerID": "local/ollama",
        "error": {
            "name": "ContextOverflowError",
            "data": {
                "message": "Session too large to compact - context exceeds model limit even after stripping media"
            },
        },
        "finish": "error",
    },
    "parts": [],
}


class ExtractReplyErrorDetectionTests(unittest.TestCase):
    def test_real_context_overflow_response_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            extract_reply(REAL_CONTEXT_OVERFLOW_RESPONSE)
        self.assertIn("ContextOverflowError", str(ctx.exception))
        self.assertIn("context exceeds model limit", str(ctx.exception))

    def test_normal_response_still_extracts_correctly(self):
        resp = {"parts": [{"type": "text", "text": "Hello!"}], "info": {"finish": "stop"}}
        text, tools = extract_reply(resp)
        self.assertEqual(text, "Hello!")
        self.assertEqual(tools, [])

    def test_response_with_no_info_field_still_works(self):
        # Older/defensive shape -- info missing entirely shouldn't crash
        # the new check, just skip it.
        resp = {"parts": [{"type": "text", "text": "still works"}]}
        text, _ = extract_reply(resp)
        self.assertEqual(text, "still works")

    def test_finish_error_with_missing_error_field_does_not_crash(self):
        # Defensive: finish=="error" but no structured error info at all.
        resp = {"parts": [], "info": {"finish": "error"}}
        with self.assertRaises(RuntimeError) as ctx:
            extract_reply(resp)
        self.assertIn("unknown error", str(ctx.exception))

    def test_finish_stop_with_empty_parts_does_not_raise(self):
        # A genuinely empty-but-successful response (finish != "error")
        # should NOT be treated as an error -- only the error case is new.
        resp = {"parts": [], "info": {"finish": "stop"}}
        text, tools = extract_reply(resp)
        self.assertEqual(text, "")
        self.assertEqual(tools, [])


if __name__ == "__main__":
    unittest.main()
