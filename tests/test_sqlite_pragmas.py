#!/usr/bin/env python3
"""Unit tests for MAILROOM §9 SQLite PRAGMAs. Temp DB only — no SoR."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sqlite_pragmas as pragmas  # noqa: E402


def _pragma(conn: sqlite3.Connection, name: str):
    return conn.execute("PRAGMA %s" % name).fetchone()[0]


class PragmaApplyTests(unittest.TestCase):
    def test_writer_pragmas(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = sqlite3.connect(str(db))
            try:
                pragmas.apply_writer_pragmas(conn)
                self.assertEqual(str(_pragma(conn, "journal_mode")).lower(), "wal")
                self.assertEqual(int(_pragma(conn, "synchronous")), 2)  # FULL
                self.assertEqual(int(_pragma(conn, "busy_timeout")), 30000)
                self.assertEqual(int(_pragma(conn, "foreign_keys")), 1)
                self.assertEqual(int(_pragma(conn, "mmap_size")), 0)
                self.assertEqual(int(_pragma(conn, "temp_store")), 2)  # MEMORY
            finally:
                conn.close()

    def test_reader_pragmas(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = sqlite3.connect(str(db))
            try:
                pragmas.apply_reader_pragmas(conn)
                self.assertEqual(str(_pragma(conn, "journal_mode")).lower(), "wal")
                self.assertEqual(int(_pragma(conn, "synchronous")), 1)  # NORMAL
                self.assertEqual(int(_pragma(conn, "busy_timeout")), 30000)
                self.assertEqual(int(_pragma(conn, "foreign_keys")), 1)
                self.assertEqual(int(_pragma(conn, "mmap_size")), 0)
                self.assertEqual(int(_pragma(conn, "temp_store")), 2)
            finally:
                conn.close()

    def test_writer_and_reader_differ_only_on_synchronous(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = sqlite3.connect(str(db))
            try:
                pragmas.apply_writer_pragmas(conn)
                writer = pragmas.read_pragmas(conn)
                pragmas.apply_reader_pragmas(conn)
                reader = pragmas.read_pragmas(conn)
            finally:
                conn.close()
        self.assertEqual(writer["synchronous"], 2)
        self.assertEqual(reader["synchronous"], 1)
        for key in ("journal_mode", "busy_timeout", "foreign_keys", "mmap_size", "temp_store"):
            self.assertEqual(writer[key], reader[key], key)


class WriterPinTests(unittest.TestCase):
    def test_min_tuple(self):
        self.assertEqual(pragmas.WRITER_SQLITE_MIN, (3, 51, 3))

    def test_this_ci_python_reports_version(self):
        ver = pragmas.sqlite_version_tuple()
        self.assertGreaterEqual(len(ver), 3)
        # CI Ubuntu python sqlite is typically ≥ 3.37; pin check is informational here.
        self.assertTrue(isinstance(pragmas.writer_sqlite_ok(), bool))


class CliTests(unittest.TestCase):
    def test_apply_writer_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            sqlite3.connect(str(db)).close()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "sqlite_pragmas.py"),
                    "--db",
                    str(db),
                    "--apply",
                    "writer",
                    "--show",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("journal_mode=wal", proc.stdout.lower())
            self.assertIn("synchronous=2", proc.stdout)
            self.assertIn("busy_timeout=30000", proc.stdout)
            self.assertIn("mmap_size=0", proc.stdout)

    def test_check_writer_version_exits_int(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "sqlite_pragmas.py"), "--check-writer-version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn(proc.returncode, (0, 2))
        self.assertIn("sqlite", proc.stdout)
        self.assertIn("3.51.3", proc.stdout)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
