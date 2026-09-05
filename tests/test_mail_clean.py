#!/usr/bin/env python3
"""Quote-strip + signature-strip unit tests. No network, no SoR."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mail_clean as mc  # noqa: E402


class QuoteStripTests(unittest.TestCase):
    def test_keeps_new_body_drops_gt_quotes(self):
        raw = "Please send the invoice.\n\n> old line\n> another\n"
        self.assertEqual(mc.clean_body(raw), "Please send the invoice.")

    def test_on_wrote_split(self):
        raw = (
            "Sounds good.\n"
            "On Tue, 1 Apr 2025 at 09:00, Ada <ada@example.com> wrote:\n"
            "> earlier\n"
        )
        self.assertEqual(mc.clean_body(raw), "Sounds good.")

    def test_original_message_split(self):
        raw = "New bit.\n-----Original Message-----\nFrom: Bob\nOld bit\n"
        self.assertEqual(mc.clean_body(raw), "New bit.")

    def test_gmail_underscore_split(self):
        raw = "Top.\n__________\nFrom: x\n"
        self.assertEqual(mc.clean_body(raw), "Top.")

    def test_forwarded_split(self):
        raw = "FYI\nBegin forwarded message:\nFrom: x\n"
        self.assertEqual(mc.clean_body(raw), "FYI")

    def test_raw_body_not_mutated_by_helper(self):
        raw = "Keep me\n> quoted"
        cleaned = mc.clean_body(raw)
        self.assertEqual(cleaned, "Keep me")
        self.assertIn("> quoted", raw)


class SignatureStripTests(unittest.TestCase):
    def test_rfc3676_delimiter(self):
        raw = "Thanks.\n-- \nAda Lovelace\nEngineer\n"
        self.assertEqual(mc.clean_body(raw), "Thanks.")

    def test_sent_from_iphone(self):
        raw = "On my way.\nSent from my iPhone\n"
        self.assertEqual(mc.clean_body(raw), "On my way.")

    def test_quote_then_signature(self):
        raw = (
            "Done.\n"
            "-- \nAda\n"
            "On Tue, Ada wrote:\n"
            "> old\n"
        )
        self.assertEqual(mc.clean_body(raw), "Done.")


class ContentHashTests(unittest.TestCase):
    def test_stable_sha256(self):
        cleaned = mc.clean_body("Hello\n> quote")
        self.assertEqual(cleaned, "Hello")
        digest = mc.content_hash(cleaned)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, mc.content_hash("Hello"))
        self.assertNotEqual(digest, mc.content_hash("Hello."))


if __name__ == "__main__":
    unittest.main()
