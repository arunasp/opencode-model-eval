#!/usr/bin/env python3
"""Unit tests for read_local_ollama_models.py's JSONC comment stripper --
specifically the exact failure mode this exists to fix: plain json.load()
breaks on the real global config's `//` comments, and a naive `//.*`
regex breaks on `//` inside a string value (hit both bugs for real this
session before landing on a proper string-aware state machine).
"""
import unittest

from read_local_ollama_models import strip_jsonc_comments, load_local_ollama_models


class StripJsoncCommentsTests(unittest.TestCase):
    def test_line_comment_removed(self):
        self.assertEqual(strip_jsonc_comments('{"a": 1}\n// comment\n'), '{"a": 1}\n\n')

    def test_block_comment_removed(self):
        self.assertEqual(strip_jsonc_comments('{"a": /* c */ 1}'), '{"a":  1}')

    def test_url_with_double_slash_preserved(self):
        # The exact real bug: a naive //.* regex eats the rest of this line.
        text = '{"$schema": "https://opencode.ai/config.json"}'
        self.assertEqual(strip_jsonc_comments(text), text)

    def test_comment_after_url_on_same_line_removed(self):
        text = '{"a": "https://x.com/y"} // trailing comment\n'
        expected = '{"a": "https://x.com/y"} \n'
        self.assertEqual(strip_jsonc_comments(text), expected)

    def test_escaped_quote_in_string_does_not_break_state(self):
        text = r'{"a": "before \" // still a string"}'
        self.assertEqual(strip_jsonc_comments(text), text)


class LoadLocalOllamaModelsTests(unittest.TestCase):
    def test_real_shape_with_comments_and_url(self):
        import tempfile
        content = '''{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local/ollama": {
      "models": {
        "gemma4:31b": {},
        // a real comment
        "qwen2.5-coder:7b": {}
      }
    }
  }
}'''
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(content)
            path = f.name
        self.assertEqual(
            load_local_ollama_models(path),
            ["gemma4:31b", "qwen2.5-coder:7b"],
        )


if __name__ == "__main__":
    unittest.main()
