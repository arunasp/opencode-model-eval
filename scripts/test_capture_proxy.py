#!/usr/bin/env python3
"""Unit tests for capture_proxy.py's pure helpers.

The redaction tests are the ones that matter most: this repo is public,
results/ is bind-mounted, and a captured provider request carries
credentials in plain text. A regression there leaks keys into an
artifact directory, silently, and nothing else in the pipeline would
notice.

The hop-by-hop tests encode RFC 9110 s7.6.1, including the part that is
easy to miss -- Connection names ADDITIONAL fields that are themselves
single-hop, and forwarding one of those breaks the next connection's
framing somewhere else entirely.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from capture_proxy import (  # noqa: E402
    HOP_BY_HOP,
    decode_body,
    is_secret_header,
    redact_headers,
    strip_hop_by_hop,
)


class RedactionTests(unittest.TestCase):

    def test_authorization_value_never_survives(self):
        out = redact_headers([("Authorization", "Bearer sk-live-abcdef123456")])
        self.assertEqual(out["Authorization"], "<redacted>")
        self.assertNotIn("sk-live-abcdef123456", str(out))

    def test_vendor_spellings(self):
        """Providers disagree about what to call this header, so matching
        one exact name would leak the others."""
        for name in ("api-key", "X-Api-Key", "x-goog-api-key", "OpenAI-Api-Key",
                     "X-Auth-Token", "Proxy-Authorization", "Cookie"):
            with self.subTest(header=name):
                self.assertTrue(is_secret_header(name), f"{name} must be treated as secret")
                self.assertEqual(redact_headers([(name, "value")])[name], "<redacted>")

    def test_case_insensitivity(self):
        for name in ("AUTHORIZATION", "authorization", "AuThOrIzAtIoN"):
            self.assertEqual(redact_headers([(name, "x")])[name], "<redacted>")

    def test_ordinary_headers_are_preserved(self):
        """The NAMES and values of non-secret headers are evidence --
        content-type decides how the body is decoded, and user-agent
        identifies the client build."""
        out = redact_headers([("Content-Type", "application/json"),
                              ("User-Agent", "opencode/1.18.23")])
        self.assertEqual(out["Content-Type"], "application/json")
        self.assertEqual(out["User-Agent"], "opencode/1.18.23")

    def test_header_names_are_kept_even_when_redacted(self):
        """Which auth scheme was used is worth recording; the value is
        not. Dropping the header entirely would lose that."""
        self.assertIn("Authorization", redact_headers([("Authorization", "Bearer x")]))


class HopByHopTests(unittest.TestCase):

    def test_fixed_set_is_dropped(self):
        headers = [(name.title(), "v") for name in HOP_BY_HOP] + [("Accept", "*/*")]
        self.assertEqual(strip_hop_by_hop(headers), [("Accept", "*/*")])

    def test_fields_named_by_connection_are_dropped(self):
        """RFC 9110 s7.6.1 -- the subtle half. Forwarding a field that
        Connection declared single-hop is the classic proxy bug."""
        headers = [("Connection", "keep-alive, X-Custom-Hop"),
                   ("X-Custom-Hop", "v"), ("Accept", "*/*")]
        self.assertEqual(strip_hop_by_hop(headers), [("Accept", "*/*")])

    def test_connection_token_matching_is_case_insensitive(self):
        headers = [("Connection", "X-UPPER"), ("x-upper", "v"), ("Accept", "*/*")]
        self.assertEqual(strip_hop_by_hop(headers), [("Accept", "*/*")])

    def test_end_to_end_headers_survive(self):
        headers = [("Content-Type", "application/json"), ("Content-Length", "12")]
        self.assertEqual(strip_hop_by_hop(headers), headers)


class DecodeBodyTests(unittest.TestCase):

    def test_json_is_parsed(self):
        parsed, reason = decode_body(b'{"stream": true}', "application/json")
        self.assertEqual(parsed, {"stream": True})
        self.assertIsNone(reason)

    def test_charset_suffix_still_counts_as_json(self):
        parsed, _ = decode_body(b'{"a": 1}', "application/json; charset=utf-8")
        self.assertEqual(parsed, {"a": 1})

    def test_non_json_is_declined_with_a_reason(self):
        """Never guesses. A capture that quietly corrupts what it
        captures is worse than one that says it declined."""
        parsed, reason = decode_body(b"\x00\x01\x02", "application/octet-stream")
        self.assertIsNone(parsed)
        self.assertIn("not JSON", reason)
        self.assertIn("3 bytes", reason)

    def test_malformed_json_reports_rather_than_raises(self):
        parsed, reason = decode_body(b'{"unterminated": ', "application/json")
        self.assertIsNone(parsed)
        self.assertIn("undecodable JSON", reason)

    def test_empty_body_is_stated(self):
        parsed, reason = decode_body(b"", "application/json")
        self.assertIsNone(parsed)
        self.assertEqual(reason, "empty body")

    def test_missing_content_type_is_stated(self):
        parsed, reason = decode_body(b"x", "")
        self.assertIsNone(parsed)
        self.assertIn("no content-type", reason)


if __name__ == "__main__":
    unittest.main()
