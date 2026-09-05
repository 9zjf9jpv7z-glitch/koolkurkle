#!/usr/bin/env python3
"""Unit tests for ask_mail hybrid retrieve stub. No network."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ask_mail  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            lane TEXT,
            from_addr TEXT,
            subject TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            id UNINDEXED,
            subject,
            body
        );
        INSERT INTO messages VALUES
          ('m1', 'inbox', 'store@example.com', 'Your Apple receipt'),
          ('m2', 'auth', 'security@example.com', 'Your verification code'),
          ('m3', 'inbox', 'bank@example.com', 'Your one-time password is 123456'),
          ('m4', 'inbox', 'bills@example.com', 'Invoice due Friday');
        INSERT INTO messages_fts VALUES
          ('m1', 'Your Apple receipt', 'Thanks for your purchase of iCloud+'),
          ('m2', 'Your verification code', 'Do not share this code 998877'),
          ('m3', 'Your one-time password is 123456', 'OTP body'),
          ('m4', 'Invoice due Friday', 'Please pay invoice 44');
        """
    )
    conn.commit()
    conn.close()


class AuthShapeTests(unittest.TestCase):
    def test_lane_auth(self):
        self.assertTrue(ask_mail.is_auth_shaped("auth", "hello"))

    def test_subject_otp(self):
        self.assertTrue(ask_mail.is_auth_shaped("inbox", "Your one-time password is 12"))

    def test_normal_receipt(self):
        self.assertFalse(ask_mail.is_auth_shaped("inbox", "Your Apple receipt"))


class FtsRetrieveTests(unittest.TestCase):
    def test_skips_auth_and_auth_shaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _make_db(db)
            conn = ask_mail.connect(db)
            try:
                hits = ask_mail.fts_hits(conn, "invoice OR receipt OR verification OR password", k=10)
            finally:
                conn.close()
        ids = [h["id"] for h in hits]
        self.assertIn("m1", ids)
        self.assertIn("m4", ids)
        self.assertNotIn("m2", ids)
        self.assertNotIn("m3", ids)

    def test_merge_unique_fts_first(self):
        fts = [{"id": "a", "source": "fts", "subject": "A"}]
        vec = [
            {"id": "a", "source": "vec", "subject": "A-vec"},
            {"id": "b", "source": "vec", "subject": "B"},
        ]
        merged = ask_mail.merge_hits(fts, vec, k=5)
        self.assertEqual([m["id"] for m in merged], ["a", "b"])
        self.assertEqual(merged[0]["source"], "fts")

    def test_missing_db(self):
        rc = ask_mail.main(["--db", "/no/such/mailroom.sqlite", "hello"])
        self.assertEqual(rc, 2)

    def test_llm_not_wired(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _make_db(db)
            rc = ask_mail.main(["--db", str(db), "--llm", "invoice"])
        self.assertEqual(rc, 2)

    def test_cli_json_fts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _make_db(db)
            rc = ask_mail.main(
                ["--db", str(db), "--fts-only", "--json", "--k", "5", "receipt"]
            )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
