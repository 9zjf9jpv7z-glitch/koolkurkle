#!/usr/bin/env python3
"""Unit tests for MAILROOM §9.5 writer lock. No network, no SoR."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import with_writer_lock as wwl  # noqa: E402


def _old_payload(hours: float = 5.0) -> str:
    acquired = datetime.now(timezone.utc) - timedelta(hours=hours)
    return wwl.format_lock_payload(
        purpose="stale-holder",
        now=acquired,
        pid=999001,
        hostname="test-host",
    )


class ParseLockTests(unittest.TestCase):
    def test_roundtrip_payload(self):
        now = datetime(2026, 9, 5, 21, 52, 0, tzinfo=timezone.utc)
        raw = wwl.format_lock_payload("embed", now, 4242, "MacBook-Pro.local")
        info = wwl.parse_lock_payload(raw)
        self.assertEqual(info.pid, 4242)
        self.assertEqual(info.hostname, "MacBook-Pro.local")
        self.assertEqual(info.purpose, "embed")
        self.assertEqual(info.acquired_at, now)

    def test_parse_zulu(self):
        dt = wwl.parse_acquired_at("2026-09-05T14:52:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset(), timedelta(0))


class AcquireReleaseTests(unittest.TestCase):
    def test_acquire_writes_metadata_and_runs_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            marker = Path(tmp) / "ran"
            rc = wwl.run_with_lock(
                "unit-test",
                [sys.executable, "-c", "from pathlib import Path; Path(%r).write_text('ok')" % str(marker)],
                lock_path=lock,
                action_required_path=action,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(marker.read_text(), "ok")
            info = wwl.read_lock_info(lock)
            self.assertEqual(info.purpose, "unit-test")
            self.assertEqual(info.hostname, socket.gethostname())
            self.assertIsNotNone(info.pid)
            self.assertIsNotNone(info.acquired_at)

    def test_second_holder_refuses_without_steal(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            held = wwl.acquire_writer_lock(lock, "first")
            try:
                with self.assertRaises(wwl.WriterLockError) as ctx:
                    wwl.acquire_writer_lock(lock, "second")
                msg = str(ctx.exception)
                self.assertIn("writer lock held", msg)
                self.assertNotIn("no steal", msg)
                self.assertIn("purpose=first", msg)
                # first holder metadata must remain
                info = wwl.read_lock_info(lock)
                self.assertEqual(info.purpose, "first")
            finally:
                wwl.release_writer_lock(held)
            # after release, a new holder can take it
            held2 = wwl.acquire_writer_lock(lock, "second")
            try:
                self.assertEqual(wwl.read_lock_info(lock).purpose, "second")
            finally:
                wwl.release_writer_lock(held2)

    def test_held_over_4h_refuses_no_steal(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            held = wwl.acquire_writer_lock(lock, "long-job")
            try:
                lock.write_text(_old_payload(5.0), encoding="utf-8")
                with self.assertRaises(wwl.WriterLockError) as ctx:
                    wwl.acquire_writer_lock(lock, "thief")
                msg = str(ctx.exception)
                self.assertIn("no steal", msg)
                self.assertIn("held >", msg)
                # still the rewritten stale metadata — we did not take the lock
                info = wwl.read_lock_info(lock)
                self.assertEqual(info.purpose, "stale-holder")
                self.assertEqual(info.pid, 999001)
            finally:
                wwl.release_writer_lock(held)

    def test_action_required_blocks_lock_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            action.write_text("human: pause writers\n", encoding="utf-8")
            marker = Path(tmp) / "should-not-run"
            with self.assertRaises(wwl.WriterLockError) as ctx:
                wwl.run_with_lock(
                    "embed",
                    [sys.executable, "-c", "from pathlib import Path; Path(%r).write_text('x')" % str(marker)],
                    lock_path=lock,
                    action_required_path=action,
                )
            self.assertIn("action-required", str(ctx.exception))
            self.assertFalse(marker.exists())
            self.assertFalse(lock.exists())

    def test_command_exit_code_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "missing"
            rc = wwl.run_with_lock(
                "failing",
                [sys.executable, "-c", "raise SystemExit(7)"],
                lock_path=lock,
                action_required_path=action,
            )
            self.assertEqual(rc, 7)


class CliTests(unittest.TestCase):
    def test_cli_purpose_then_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            out = Path(tmp) / "out.txt"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "with_writer_lock.py"),
                    "--purpose",
                    "cli-probe",
                    "--lock-file",
                    str(lock),
                    "--action-required-file",
                    str(action),
                    "--",
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(%r).write_text('ran')" % str(out),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(out.read_text(), "ran")
            self.assertEqual(wwl.read_lock_info(lock).purpose, "cli-probe")

    def test_cli_stale_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            held = wwl.acquire_writer_lock(lock, "holder")
            try:
                lock.write_text(_old_payload(8.0), encoding="utf-8")
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "with_writer_lock.py"),
                        "--purpose",
                        "later",
                        "--lock-file",
                        str(lock),
                        "--action-required-file",
                        str(action),
                        "--",
                        sys.executable,
                        "-c",
                        "print('should-not-run')",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertIn("no steal", proc.stderr)
                self.assertNotIn("should-not-run", proc.stdout)
            finally:
                wwl.release_writer_lock(held)


class DocsContractTests(unittest.TestCase):
    def test_pr0_docs_exist(self):
        docs = ROOT / "docs" / "pr0"
        for name in (
            "SCHEMA_DUMP_MBP.md",
            "SCHEMA_NOTE_MINI.md",
            "mailroom_schema.sql",
            "with_writer_lock_DESIGN.md",
        ):
            path = docs / name
            self.assertTrue(path.is_file(), path)
            self.assertGreater(path.stat().st_size, 40)

    def test_schema_sql_has_existing_reply_and_hash(self):
        sql = (ROOT / "docs" / "pr0" / "mailroom_schema.sql").read_text(encoding="utf-8")
        self.assertIn("in_reply_to TEXT", sql)
        self.assertIn("content_hash TEXT", sql)
        self.assertIn("embedding float[1024]", sql)
        self.assertIn("dims INTEGER NOT NULL DEFAULT 1024", sql)
        self.assertIn("PR-1 must skip", sql)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
