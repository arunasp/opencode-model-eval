#!/usr/bin/env python3
"""Regression tests for cvv_scan.py.

Stdlib-only (unittest), per CODEGEN.md's Python conventions. Currently
covers SECONDARY_RENDERING_TRUST only -- the other five patterns
predate this file and don't have committed fixtures yet; adding those
is a separate pass, not scoped here.

Usage:
    python3 scripts/tools/test_cvv_scan.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cvv_scan  # noqa: E402


POSITIVE_TRANSCRIPT = """## User
Take a look at this repo: https://github.com/example/some-repo
---
## Assistant (Build * test-model * 3.2s)
_Thinking:_
Let me check the repo.

**Tool: web_fetch**
**Input:** {"url": "https://github.com/example/some-repo"}
**Output:** "Latest commit History[1 Commit]..."

The repo history shows 1 commit total, so there's no earlier state to diff against.
---
"""

NEGATIVE_TRANSCRIPT = """## User
Actually clone it first.
---
## Assistant (Build * test-model * 5.1s)
_Thinking:_
Good point, let me clone it directly.

**Tool: bash_tool**
**Input:** {"command": "git clone https://github.com/example/some-repo"}
**Output:** "Cloning into 'some-repo'..."

**Tool: web_fetch**
**Input:** {"url": "https://github.com/example/some-repo"}
**Output:** "Latest commit History[1 Commit]..."

**Tool: bash_tool**
**Input:** {"command": "git log --oneline | wc -l"}
**Output:** "8"

The repo history shows 8 commits, confirmed via direct clone.
---
"""


class SecondaryRenderingTrustTests(unittest.TestCase):
    def test_flags_claim_grounded_only_in_a_rendered_fetch(self) -> None:
        turns = cvv_scan.split_into_assistant_turns(POSITIVE_TRANSCRIPT)
        self.assertEqual(len(turns), 1)
        base_line_no, turn_text = turns[0]
        report = cvv_scan.scan_turn(1, base_line_no, turn_text)
        categories = [f.category for f in report.findings]
        self.assertIn("SECONDARY_RENDERING_TRUST", categories)

    def test_does_not_flag_when_direct_access_precedes_the_claim(self) -> None:
        turns = cvv_scan.split_into_assistant_turns(NEGATIVE_TRANSCRIPT)
        self.assertEqual(len(turns), 1)
        base_line_no, turn_text = turns[0]
        report = cvv_scan.scan_turn(1, base_line_no, turn_text)
        categories = [f.category for f in report.findings]
        self.assertNotIn("SECONDARY_RENDERING_TRUST", categories)

    KNOWN_GAP_TRANSCRIPT = """## User
Fetch the repo page, then tell me the commit count.
---
## Assistant (Build * test-model * 2.0s)
_Thinking:_
Checking the page.

**Tool: web_fetch**
**Input:** {"url": "https://github.com/example/some-repo"}
**Output:** "Latest commit History[1 Commit]..."

The repo history shows 1 commit total.
---
## User
Are you sure? Clone it and check for real this time.
---
## Assistant (Build * test-model * 4.0s)
_Thinking:_
Fair, let me actually clone it.

**Tool: bash_tool**
**Input:** {"command": "git clone https://github.com/example/some-repo && git log --oneline | wc -l"}
**Output:** "8"

Confirmed -- it's actually 8 commits, my earlier read of the page was wrong.
---
"""

    def test_known_gap_cross_turn_repeated_claim_not_caught(self) -> None:
        """Documents an honest limit -- doesn't pretend it's covered.

        The real incident this pattern is named after was exactly this
        shape: web_fetch and a wrong claim in turn 1, the correction and
        real clone only in turn 2. Turn 1 on its own correctly flags.
        Turn 2 on its own correctly does NOT flag (direct access
        precedes its claim). Neither turn is wrong in isolation -- the
        actual gap is that nothing links turn 1's flagged claim to
        turn 2's correction, so a scanner run turn-by-turn never
        surfaces "this got corrected two turns later" as a pattern of
        its own. Cross-turn artifact-identity tracking would be needed
        to close this; out of scope for this pass, asserted here so
        it isn't lost.
        """
        turns = cvv_scan.split_into_assistant_turns(self.KNOWN_GAP_TRANSCRIPT)
        self.assertEqual(len(turns), 2)

        base_line_no_1, turn_text_1 = turns[0]
        report_1 = cvv_scan.scan_turn(1, base_line_no_1, turn_text_1)
        categories_1 = [f.category for f in report_1.findings]
        self.assertIn(
            "SECONDARY_RENDERING_TRUST",
            categories_1,
            "turn 1's ungrounded claim should still be caught on its own",
        )

        base_line_no_2, turn_text_2 = turns[1]
        report_2 = cvv_scan.scan_turn(2, base_line_no_2, turn_text_2)
        categories_2 = [f.category for f in report_2.findings]
        self.assertNotIn(
            "SECONDARY_RENDERING_TRUST",
            categories_2,
            "turn 2 is clean in isolation -- the gap is that nothing "
            "carries turn 1's flag forward or links it to turn 2's "
            "correction, not that turn 2 itself is wrong",
        )


if __name__ == "__main__":
    unittest.main()
