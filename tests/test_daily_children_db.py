#!/usr/bin/env python3
"""Offline SoR bind for daily children. No IMAP, no Keychain, no PII."""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import classify  # noqa: E402
import imap_fetch_bodies_fts  # noqa: E402
import imap_newmail  # noqa: E402
import imap_tombstone  # noqa: E402
import mailroom_copy_db as copy_db  # noqa: E402
import notify_bills  # noqa: E402

CHILD_MODULES = (
    imap_newmail,
    imap_tombstone,
    imap_fetch_bodies_fts,
    classify,
    notify_bills,
)
CHILD_NAMES = (
    "imap_newmail.py",
    "imap_tombstone.py",
    "imap_fetch_bodies_fts.py",
    "classify.py",
    "notify_bills.py",
)
COPY_A = "mailroom-copy.sqlite"
COPY_B = "mailroom-daily-copy.sqlite"
SOR = "mailroom.sqlite"


class ArgvNoneHypothesisTests(unittest.TestCase):
    """bind_copy_db / parse_db_cli must see sys.argv[1:] when argv is None."""

    def test_parse_db_cli_none_reads_sys_argv_tail(self):
        copy = "/tmp/%s" % COPY_A
        with patch.object(sys, "argv", ["imap_tombstone.py", "--db", copy, "--other"]):
            self.assertEqual(copy_db.parse_db_cli(None), copy)

    def test_parse_db_cli_empty_list_is_explicit_no_flags(self):
        with patch.object(sys, "argv", ["prog", "--db", "/tmp/%s" % COPY_A]):
            self.assertIsNone(copy_db.parse_db_cli([]))

    def test_bind_copy_db_none_honors_process_db_flag(self):
        copy = "/tmp/%s" % COPY_B
        with patch.object(sys, "argv", ["classify.py", "--db", copy]):
            path = copy_db.bind_copy_db(None)
        self.assertEqual(str(path), copy)
        self.assertEqual(os.environ.get("MAILROOM_DB"), copy)

    def test_bind_copy_db_none_ignoring_sys_argv_is_the_bug(self):
        """FAIL if None is treated as [] and --db on sys.argv is dropped."""
        src = inspect.getsource(copy_db.parse_db_cli)
        self.assertIn("sys.argv[1:]", src)
        self.assertIn("argv is None", src)
        self.assertNotIn(
            "if not argv:",
            src,
            msg="if not argv treats None like [] and ignores process --db",
        )

    def test_tombstone_bind_copy_db_is_shared_helper(self):
        self.assertIs(imap_tombstone.bind_copy_db, copy_db.bind_copy_db)
        copy = "/tmp/%s" % COPY_A
        with patch.object(sys, "argv", ["imap_tombstone.py", "--db", copy]):
            path = imap_tombstone.bind_copy_db()
        self.assertEqual(path.name, COPY_A)


class ChildHonorTests(unittest.TestCase):
    """Interface proof: every daily child honors --db and MAILROOM_DB."""

    def test_each_child_main_honors_db_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / COPY_A
            for mod in CHILD_MODULES:
                with self.subTest(mod=mod.__name__):
                    rc = mod.main(["--db", str(db)])
                    self.assertEqual(rc, 0)
                    self.assertEqual(os.environ.get("MAILROOM_DB"), str(db))

    def test_each_child_main_honors_env_when_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / COPY_B
            env = {k: v for k, v in os.environ.items() if k != "MAILROOM_DB"}
            env["MAILROOM_DB"] = str(db)
            for mod in CHILD_MODULES:
                with self.subTest(mod=mod.__name__):
                    with patch.dict(os.environ, env, clear=True):
                        rc = mod.main([])
                    self.assertEqual(rc, 0)

    def test_cli_interface_proof_copy_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in CHILD_NAMES:
                for basename in (COPY_A, COPY_B):
                    db = Path(tmp) / basename
                    proc = subprocess.run(
                        [sys.executable, str(SCRIPTS / name), "--db", str(db)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("db_mode=copy", proc.stderr)
                    self.assertIn("opened_db=%s" % db, proc.stdout)
                    self.assertNotIn("db_mode=refused", proc.stderr)

    def test_cli_interface_proof_mailroom_db_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / COPY_A
            env = {**os.environ, "MAILROOM_DB": str(db)}
            for name in CHILD_NAMES:
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / name)],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("db_mode=copy", proc.stderr)
                self.assertIn("opened_db=%s" % db, proc.stdout)


class ChildRefuseTests(unittest.TestCase):
    """Negative smoke: SoR basename and unset fail closed."""

    def test_refuse_sor_basename_via_db_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / SOR
            for name in CHILD_NAMES:
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / name), "--db", str(db)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(proc.returncode, 0, name)
                self.assertEqual(proc.returncode, 2, name)
                self.assertIn("db_mode=refused", proc.stderr)
                self.assertIn(SOR, proc.stderr)
                self.assertNotIn("db_mode=copy", proc.stderr)
                self.assertNotIn("opened_db=", proc.stdout)

    def test_refuse_sor_basename_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / SOR
            env = {**os.environ, "MAILROOM_DB": str(db)}
            for name in CHILD_NAMES:
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / name)],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
                self.assertEqual(proc.returncode, 2, name)
                self.assertIn("db_mode=refused", proc.stderr)
                self.assertNotIn("db_mode=copy", proc.stderr)

    def test_refuse_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "MAILROOM_DB"}
        for name in CHILD_NAMES:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 2, name)
            self.assertIn("db_mode=refused", proc.stderr)
            self.assertIn("unset", proc.stderr)
            self.assertNotIn("db_mode=copy", proc.stderr)

    def test_negative_smoke_fails_if_child_opens_sor_or_defaults(self):
        """FAIL if a child would accept mailroom.sqlite or a silent default."""
        self.assertNotIn(SOR, copy_db.COPY_DB_BASENAMES)
        with tempfile.TemporaryDirectory() as tmp:
            sor = Path(tmp) / SOR
            copy = Path(tmp) / COPY_A
            for name in CHILD_NAMES:
                src = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("bind_copy_db", src, msg=name)
                self.assertIn("mailroom_copy_db", src, msg=name)
                self.assertNotIn(
                    "MailArchive/mailroom.sqlite",
                    src,
                    msg="%s hardcodes the SoR path" % name,
                )
                self.assertTrue(
                    copy_db.child_would_open_sor(
                        ["--db", str(sor)], {"MAILROOM_DB": str(copy)}
                    ),
                    msg=name,
                )
                self.assertFalse(
                    copy_db.child_would_open_sor(
                        ["--db", str(copy)], {"MAILROOM_DB": str(sor)}
                    ),
                    msg=name,
                )


class ChildSourceHygieneTests(unittest.TestCase):
    def test_children_are_bind_only_no_live_imap_or_pii(self):
        forbidden = (
            "EXAMPLE_USER_LOCAL",
            "@example.invalid",
            "-----BEGIN",
            "ak_live",
            "imap.mail.me.com",
            "IMAP_APP_PASSWORD",
            "find-generic-password",
        )
        for name in CHILD_NAMES:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, msg="%s %s" % (name, token))
            self.assertIn("bind_copy_db", text)
            self.assertIn("fail closed", text)

    def test_tombstone_exports_bind_copy_db(self):
        self.assertTrue(hasattr(imap_tombstone, "bind_copy_db"))
        self.assertTrue(callable(imap_tombstone.bind_copy_db))
        reloaded = importlib.reload(imap_tombstone)
        self.assertIs(reloaded.bind_copy_db, copy_db.bind_copy_db)


if __name__ == "__main__":
    unittest.main()
