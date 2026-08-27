#!/usr/bin/env python3
"""Regression tests for _summary_note_for_category().

Real bug found by correlating a real completed run's full report.json
against its own console summary output: every one of 9 categories
failed via a genuine context-overflow error (opencode server log
error, confirmed live), yet the printed summary said "[stopped: CVV
violation]" for all of them -- the "opencode server log error" reason
prefix (added alongside the server-log-error detection in an earlier
hotfix) was never added to this classification, so nothing matched and
every one silently fell through to the generic CVV-violation fallback.

scripts/testdata/real_all_categories_context_overflow_report.json is
the exact real report.json from that run, used here directly rather
than a synthetic approximation.

Usage:
    python3 scripts/test_summary_note_classification.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval_client import _summary_note_for_category  # noqa: E402

REAL_REPORT_PATH = Path(__file__).resolve().parent / "testdata" / "real_all_categories_context_overflow_report.json"


class SummaryNoteClassificationTests(unittest.TestCase):
    def test_passing_category_has_no_note(self):
        cat_report = {"tiers": [{"passed": True}]}
        self.assertEqual(_summary_note_for_category(cat_report), "")

    def test_empty_tiers_has_no_note(self):
        self.assertEqual(_summary_note_for_category({"tiers": []}), "")

    def test_needs_manual_review(self):
        cat_report = {"tiers": [{"passed": False, "needs_manual_review": True}]}
        self.assertEqual(_summary_note_for_category(cat_report), " [stopped: NEEDS MANUAL REVIEW]")

    def test_quota_exhausted(self):
        cat_report = {"tiers": [{
            "passed": False,
            "reason": "quota/rate-limit exhausted: rate_limited -- quota exceeded",
            "quota_wait_seconds": 3600,
        }]}
        self.assertEqual(_summary_note_for_category(cat_report),
                         " [stopped: QUOTA -- next opencode attempt in ~60min, gave up waiting]")

    def test_server_log_error(self):
        # The exact bug this test guards against: this reason prefix
        # must classify as SERVER ERROR, not fall through to the
        # generic CVV-violation default.
        cat_report = {"tiers": [{
            "passed": False,
            "reason": "opencode server log error: context overflow (...): some log line",
        }]}
        self.assertEqual(_summary_note_for_category(cat_report), " [stopped: SERVER ERROR]")

    def test_http_request_error(self):
        cat_report = {"tiers": [{
            "passed": False,
            "reason": "HTTP/request error: timed out after 300s",
        }]}
        self.assertEqual(_summary_note_for_category(cat_report), " [stopped: ERROR]")

    def test_genuine_cvv_violation_still_classifies_correctly(self):
        # A real capability FAIL (the model answered, CVV scoring found
        # a violation) must still show as CVV violation -- this
        # classification isn't being removed, just no longer applied
        # to reasons it doesn't actually match.
        cat_report = {"tiers": [{
            "passed": False,
            "reason": "CVV violation: unbacked claim detected",
        }]}
        self.assertEqual(_summary_note_for_category(cat_report), " [stopped: CVV violation]")

    def test_real_report_all_nine_categories_classify_as_server_error(self):
        report = json.loads(REAL_REPORT_PATH.read_text())
        self.assertEqual(len(report["categories"]), 9)
        for cat_report in report["categories"]:
            note = _summary_note_for_category(cat_report)
            self.assertEqual(
                note, " [stopped: SERVER ERROR]",
                f"{cat_report['category']}: expected SERVER ERROR, got {note!r} "
                f"(this is the exact real bug -- every one of these was previously "
                f"mislabeled as a CVV violation)",
            )


if __name__ == "__main__":
    unittest.main()
