#!/usr/bin/env python3
"""Regression tests for axiom_cvv_verify.py's TF-IDF-based semantic
action-detection fallback -- replaces an earlier ONNX-transformer
implementation that depended on onnxruntime, which has zero published
wheels for musllinux and/or Python 3.14 (confirmed live in this
project's own container; microsoft/onnxruntime#25737, still open).

Same architecture as the original (embed -> compare against fixed
action/narration exemplar centroids via cosine similarity), same call
contract (ACTION_FALLBACK_AVAILABLE, _action_score(), ACTION_THRESHOLD)
-- only the embedding mechanism changed, from a learned transformer
embedding to TF-IDF over a fixed vocabulary built from the exemplar
sentences. Needs nothing beyond numpy, so ACTION_FALLBACK_AVAILABLE is
now unconditionally True -- no environment-dependent skip needed.

Usage:
    python3 scripts/tools/test_axiom_cvv_action_detection.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import axiom_cvv_verify as a  # noqa: E402


class ActionDetectionTests(unittest.TestCase):
    def test_fallback_always_available(self):
        # No external runtime/model download needed anymore -- this
        # should never be False.
        self.assertTrue(a.ACTION_FALLBACK_AVAILABLE)

    def test_exact_action_exemplar_scores_positive(self):
        score = a._action_score("I ran the command and checked the output.")
        self.assertGreater(score, a.ACTION_THRESHOLD)

    def test_exact_narration_exemplar_scores_negative(self):
        score = a._action_score("I think this is probably correct.")
        self.assertLess(score, a.ACTION_THRESHOLD)

    def test_novel_action_sentence_generalizes(self):
        # Different words entirely from any exemplar -- tests that
        # shared content-word overlap (not just exact exemplar match)
        # is enough to classify correctly.
        score = a._action_score(
            "I pulled up the actual log file and read through it line by line."
        )
        self.assertGreater(score, a.ACTION_THRESHOLD)

    def test_novel_narration_sentence_generalizes(self):
        score = a._action_score(
            "This is likely fine based on how these things usually work."
        )
        self.assertLess(score, a.ACTION_THRESHOLD)

    def test_different_marker_syntax_for_real_action(self):
        # The exact motivating bug this whole fallback exists to fix:
        # backed_ratio silently collapsed to 0.0 on a functionally
        # identical transcript that used different tool-call marker
        # syntax -- a marker-only regex has zero tolerance for this,
        # TF-IDF's shared-vocabulary matching should not.
        score = a._action_score(
            "Ran grep against the codebase and confirmed the exact match."
        )
        self.assertGreater(score, a.ACTION_THRESHOLD)

    def test_zero_vocabulary_overlap_fails_safely_not_falsely_positive(self):
        # Disclosed limitation, tested explicitly: a sentence sharing
        # no vocabulary with either exemplar set embeds to the
        # all-zero vector -- score must be exactly 0 (not counted as
        # an action), never a false positive.
        score = a._action_score("xyzzy plugh foobar quux")
        self.assertEqual(score, 0.0)
        self.assertFalse(score > a.ACTION_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
