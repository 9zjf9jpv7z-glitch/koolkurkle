#!/usr/bin/env python3
"""Thread-graph unit tests. No network, no SoR."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import thread_graph as tg  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_extracts_angle_ids(self):
        refs = "<root@x> <mid1@x> <parent@x>"
        self.assertEqual(
            tg.message_id_tokens(refs),
            ["<root@x>", "<mid1@x>", "<parent@x>"],
        )

    def test_wraps_bare_id(self):
        self.assertEqual(tg.normalize_message_id("a@b"), "<a@b>")
        self.assertEqual(tg.normalize_message_id("<a@b>"), "<a@b>")


class ThreadFieldTests(unittest.TestCase):
    def test_root_is_first_references(self):
        fields = tg.thread_fields(
            message_id="m1",
            message_id_header="<this@x>",
            in_reply_to="<parent@x>",
            references_header="<root@x> <parent@x>",
        )
        self.assertEqual(fields["thread_id"], "<root@x>")
        self.assertEqual(fields["in_reply_to"], "<parent@x>")
        self.assertEqual(fields["references_header"], "<root@x> <parent@x>")

    def test_falls_back_to_in_reply_to(self):
        fields = tg.thread_fields(
            message_id="m1",
            message_id_header="<this@x>",
            in_reply_to="<parent@x>",
            references_header=None,
        )
        self.assertEqual(fields["thread_id"], "<parent@x>")

    def test_falls_back_to_own_message_id(self):
        fields = tg.thread_fields(
            message_id="m1",
            message_id_header="<this@x>",
        )
        self.assertEqual(fields["thread_id"], "<this@x>")
        self.assertIsNone(fields["in_reply_to"])

    def test_falls_back_to_row_id(self):
        fields = tg.thread_fields(message_id="row-9")
        self.assertEqual(fields["thread_id"], "row-9")

    def test_from_row_mapping(self):
        fields = tg.thread_fields_from_row(
            {
                "id": "m1",
                "message_id_header": "<this@x>",
                "in_reply_to": "<p@x>",
                "references_header": "<r@x> <p@x>",
            }
        )
        self.assertEqual(fields["thread_id"], "<r@x>")


if __name__ == "__main__":
    unittest.main()
