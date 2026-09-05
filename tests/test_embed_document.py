#!/usr/bin/env python3
"""Header-prefixed embed document tests. No network, no SoR."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_document as ed  # noqa: E402


class DocumentTests(unittest.TestCase):
    def test_exact_header_block(self):
        text = ed.document_embed_text(
            subject="Invoice",
            from_addr="ada@example.com",
            from_name="Ada",
            to_addrs="bob@example.com",
            date_iso="2026-09-05T20:00:00Z",
            lane="inbox",
            cleaned_body="Please pay.",
        )
        self.assertEqual(
            text,
            "Subject: Invoice\n"
            "From: Ada <ada@example.com>\n"
            "To: bob@example.com\n"
            "Date: 2026-09-05T20:00:00Z\n"
            "Lane: inbox\n"
            "\n"
            "Please pay.",
        )

    def test_empty_fields_still_prefixed(self):
        text = ed.document_embed_text(cleaned_body="Hi")
        self.assertTrue(text.startswith("Subject: \nFrom: \nTo: \nDate: \nLane: \n\nHi"))

    def test_date_naive_becomes_zulu(self):
        text = ed.document_embed_text(date_iso="2026-09-05T20:00:00", cleaned_body="x")
        self.assertIn("Date: 2026-09-05T20:00:00Z", text)

    def test_cap_truncates_finished_document(self):
        text = ed.document_embed_text(
            subject="S",
            cleaned_body="ABCDEFGHIJ",
            cap=20,
        )
        self.assertEqual(len(text), 20)
        self.assertTrue(text.startswith("Subject: S"))


if __name__ == "__main__":
    unittest.main()
