#!/usr/bin/env python3
"""Doc-guard for PR-37 embed_backfill single-writer HARD DECK. No network."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMBED = ROOT / "docs" / "embed-backfill.md"
OPS = ROOT / "docs" / "ops-terminal.md"
DAILY = ROOT / "scripts" / "README.mailroom-daily.md"
README = ROOT / "README.md"
LOCK = ROOT / "docs" / "pr0" / "with_writer_lock_DESIGN.md"
HEALTH = ROOT / "docs" / "sor-health.md"
BACKFILL = ROOT / "scripts" / "embed_backfill.py"


class EmbedBackfillPracticeDocTests(unittest.TestCase):
    def test_covers_four_preferred_practices(self):
        text = EMBED.read_text(encoding="utf-8")
        self.assertIn("HARD DECK", text)
        self.assertIn("embed_backfill", text)
        self.assertIn("--lock", text)
        self.assertIn("Same-file 2-wide", text)
        self.assertIn("2-wide", text)
        self.assertIn("mailroom-copy.sqlite", text)
        self.assertIn("mailroom.sqlite", text)
        self.assertIn("embed_merge_shards.py", text)
        self.assertIn("EXIT 0", text)
        self.assertIn("merge window", text)
        self.assertIn("copy host", text)
        self.assertIn("SoR host", text)
        self.assertIn("sequential bands", text)
        self.assertIn("separate files", text)
        self.assertIn("integrity_check", text)
        self.assertIn("aside", text)
        self.assertIn("known-good", text)
        self.assertIn("merge-back", text)
        self.assertIn("ops-terminal.md", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("@me.com", text)
        self.assertNotIn("@icloud.com", text)
        self.assertNotIn("EXAMPLE_USER_LOCAL", text)

    def test_operators_see_it_before_start(self):
        for path in (OPS, DAILY, README, LOCK, HEALTH, BACKFILL):
            text = path.read_text(encoding="utf-8")
            self.assertIn("embed-backfill.md", text, msg=path.name)
            self.assertIn("HARD DECK", text, msg=path.name)

        ops = OPS.read_text(encoding="utf-8")
        self.assertIn("**before** starting", ops)
        self.assertIn("same-file 2-wide", ops)
        self.assertIn("embed_merge_shards.py", ops)
        self.assertIn("Sequential bands", ops)
        self.assertIn("malformed working copy", ops)
        self.assertIn("PRAGMA integrity_check", ops)

        daily = DAILY.read_text(encoding="utf-8")
        self.assertIn("one `embed_backfill` writer per", daily)
        self.assertIn("embed_merge_shards.py", daily)

        backfill = BACKFILL.read_text(encoding="utf-8")
        self.assertIn("One writer per .sqlite is HARD DECK", backfill)
        self.assertIn("docs/embed-backfill.md before start", backfill)


if __name__ == "__main__":
    unittest.main()
