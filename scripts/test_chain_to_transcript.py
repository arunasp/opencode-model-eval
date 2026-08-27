#!/usr/bin/env python3
"""Unit tests for chain_to_transcript().

The transcript is what cvv_scan.py reads, so what reaches it decides
every verdict. The assembly this replaces could not be tested at all --
it took two live response objects and a session-wide tool list, and got
the attribution wrong in a way no fixture could expose. Building from
the captured chain makes it a pure function over recorded data, which
is the only reason these tests can exist.

The fixtures below are shaped like a real chain: a list of messages,
each {"info": {...}, "parts": [...]}, with children keyed by session id.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_eval_client as rec  # noqa: E402
from run_eval_client import chain_to_transcript  # noqa: E402


def user(text):
    return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}


def assistant(parts, finish="stop"):
    return {"info": {"role": "assistant", "finish": finish}, "parts": parts}


def tool_part(name, status="completed", inp=None, out=None):
    return {"type": "tool", "tool": name,
            "state": {"status": status, "input": inp or {}, "output": out or ""}}


class ChainToTranscriptTests(unittest.TestCase):

    def test_tool_calls_land_on_the_turn_that_made_them(self):
        """The defect this replaces: the scored turn's tool calls were
        taken from the final response object, where they do not appear,
        while the session-wide capture was attributed to the setup turn
        -- and discarded entirely if the setup turn had any tool of its
        own. Here each part renders under its own message."""
        chain = {"messages": [
            user("setup"),
            assistant([tool_part("read", inp={"path": "a.txt"}, out="alpha"),
                       {"type": "text", "text": "setup done"}]),
            user("probe"),
            assistant([tool_part("grep", inp={"pattern": "needle"}, out="haystack:12"),
                       {"type": "text", "text": "found it at line 12"}]),
        ], "children": {}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("**Tool: read**", out)
        self.assertIn("**Tool: grep**", out)
        self.assertIn("haystack:12", out)
        # The probe's tool must appear AFTER the probe's user message,
        # not folded into the setup turn.
        self.assertLess(out.index("probe"), out.index("**Tool: grep**"))
        self.assertLess(out.index("**Tool: read**"), out.index("probe"))

    def test_setup_tools_do_not_suppress_probe_tools(self):
        """The exact `setup_tools or session_tools` bug: a setup turn
        with its own tool call used to discard the whole session-wide
        capture, taking the probe's evidence with it."""
        chain = {"messages": [
            user("setup"),
            assistant([tool_part("read", out="x")]),
            user("probe"),
            assistant([tool_part("webfetch", out="page body")]),
        ], "children": {}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("**Tool: read**", out)
        self.assertIn("**Tool: webfetch**", out)

    def test_reasoning_is_kept_and_labelled(self):
        """A model that reasons at length and answers tersely used to be
        indistinguishable from one that did nothing."""
        chain = {"messages": [
            user("probe"),
            assistant([{"type": "reasoning", "text": "checking the source first"},
                       {"type": "text", "text": "no."}]),
        ], "children": {}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("**Reasoning:**", out)
        self.assertIn("checking the source first", out)

    def test_subagent_work_is_included_and_labelled(self):
        """The `task` tool puts the real work in a child session. A
        scanner reading only the parent sees a model that did nothing."""
        chain = {"messages": [
            user("probe"),
            assistant([tool_part("task", status="completed", out="delegated")]),
        ], "children": {"ses_child1": [
            user("subagent instructions"),
            assistant([tool_part("grep", out="match at 40"),
                       {"type": "text", "text": "child answer"}]),
        ]}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("# Subagent session ses_child1", out)
        self.assertIn("match at 40", out)
        self.assertIn("child answer", out)

    def test_truncation_is_stated_not_silent(self):
        """A scanner cannot tell a tool that returned nothing from one
        whose output was quietly cut. The old assembly cut at 2000
        characters and said nothing."""
        long_output = "x" * (rec.TRANSCRIPT_TOOL_OUTPUT_CHARS + 500)
        chain = {"messages": [
            user("probe"),
            assistant([tool_part("read", out=long_output)]),
        ], "children": {}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("more characters", out)
        self.assertIn("raw.json", out)

    def test_capture_gap_is_stated_in_the_transcript(self):
        """An incomplete record must not read as an idle model."""
        chain = {"messages": [user("probe")], "children": {},
                 "error": "children unavailable: TimeoutError"}
        out = chain_to_transcript(chain)
        self.assertIn("CAPTURE INCOMPLETE", out)
        self.assertIn("children unavailable", out)

    def test_finish_reason_is_recorded(self):
        """finish=unknown is the signature of a provider that cannot end
        a turn -- worth being visible to whoever reads the transcript,
        not only to the detector."""
        chain = {"messages": [
            user("probe"),
            assistant([{"type": "text", "text": ""}], finish="unknown"),
        ], "children": {}, "error": None}
        self.assertIn("_[finish: unknown]_", chain_to_transcript(chain))

    def test_empty_chain_is_empty_not_an_error(self):
        self.assertEqual(chain_to_transcript({"messages": [], "children": {}, "error": None}), "")
        self.assertEqual(chain_to_transcript({}), "")

    def test_malformed_parts_are_skipped_without_raising(self):
        """Capture is best-effort upstream; a scoring run must not die
        because one part came back in an unexpected shape."""
        chain = {"messages": [
            {"info": {"role": "assistant"}, "parts": ["not a dict", None,
                                                      {"type": "text", "text": "survived"}]},
            "not a message",
        ], "children": {}, "error": None}
        out = chain_to_transcript(chain)
        self.assertIn("survived", out)


if __name__ == "__main__":
    unittest.main()
