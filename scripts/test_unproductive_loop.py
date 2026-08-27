#!/usr/bin/env python3
"""Unit tests for the unproductive-loop detector.

Pure-function tests, no server. The behaviour they encode was measured
2026-08-27 against a backend that answers a stream:true request with
non-streaming JSON: opencode keeps prompting because the provider never
reports a finish reason, producing one assistant message per provider
call with no text -- 338 messages in 24s. The point of the detector is
that this is NOT what _progress_is_moving() looks for: the counters DO
move, so the existing stall check reports the session healthy.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_eval_client import (  # noqa: E402
    _progress_is_moving,
    _unproductive_loop,
)


def snap(messages, finish_unknown, text_chars=0, reasoning_chars=0, tools=0):
    return {"messages": messages, "finish_unknown": finish_unknown,
            "text_chars": text_chars, "reasoning_chars": reasoning_chars,
            "tools": tools, "steps": messages, "output_tokens": 0}


class UnproductiveLoopTests(unittest.TestCase):

    def test_fires_on_the_measured_signature(self):
        """Messages climbing, finish=unknown piling up, text frozen."""
        before = snap(messages=120, finish_unknown=119, text_chars=5)
        after = snap(messages=340, finish_unknown=338, text_chars=5)
        self.assertTrue(_unproductive_loop(before, after))

    def test_the_stall_detector_cannot_catch_it(self):
        """The reason this predicate exists at all, asserted rather than
        described: the same pair reads as healthy movement."""
        before = snap(messages=120, finish_unknown=119, text_chars=5)
        after = snap(messages=340, finish_unknown=338, text_chars=5)
        self.assertTrue(_progress_is_moving(before, after))

    def test_quiet_below_the_threshold(self):
        """A handful of unknown finishes is not a loop."""
        before = snap(messages=3, finish_unknown=2, text_chars=5)
        after = snap(messages=8, finish_unknown=7, text_chars=5)
        self.assertFalse(_unproductive_loop(before, after))

    def test_quiet_when_text_is_arriving(self):
        """A model answering is never caught, however it reports finish."""
        before = snap(messages=120, finish_unknown=119, text_chars=400)
        after = snap(messages=340, finish_unknown=338, text_chars=9000)
        self.assertFalse(_unproductive_loop(before, after))

    def test_quiet_when_tools_are_running(self):
        before = snap(messages=120, finish_unknown=119, tools=2)
        after = snap(messages=340, finish_unknown=338, tools=7)
        self.assertFalse(_unproductive_loop(before, after))

    def test_quiet_when_reasoning_is_arriving(self):
        """A reasoning model can emit nothing but reasoning for a long
        stretch -- that is work, not a loop."""
        before = snap(messages=120, finish_unknown=119, reasoning_chars=100)
        after = snap(messages=340, finish_unknown=338, reasoning_chars=8000)
        self.assertFalse(_unproductive_loop(before, after))

    def test_quiet_on_a_slow_tier_with_no_new_turns(self):
        """Message count static is a STALL, which _progress_is_moving()
        already reports. This predicate must not also claim it, or one
        cause gets two names."""
        before = snap(messages=340, finish_unknown=338, text_chars=5)
        after = snap(messages=340, finish_unknown=338, text_chars=5)
        self.assertFalse(_unproductive_loop(before, after))
        self.assertFalse(_progress_is_moving(before, after))

    def test_quiet_on_the_first_sample(self):
        """No previous snapshot means no evidence of a trend. Never cry
        wolf on the first check of a tier."""
        self.assertFalse(_unproductive_loop({}, snap(messages=340, finish_unknown=338)))
        self.assertFalse(_unproductive_loop(snap(messages=1, finish_unknown=0), {}))

    def test_threshold_is_honoured(self):
        before = snap(messages=10, finish_unknown=9, text_chars=5)
        after = snap(messages=30, finish_unknown=29, text_chars=5)
        self.assertFalse(_unproductive_loop(before, after, threshold=40))
        self.assertTrue(_unproductive_loop(before, after, threshold=10))


if __name__ == "__main__":
    unittest.main()
