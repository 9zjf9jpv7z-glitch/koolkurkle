#!/usr/bin/env python3
"""LaunchAgent plist + README contract tests. No launchctl."""

from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "launchd" / "com.mailroom.daily.plist"
README = ROOT / "scripts" / "README.mailroom-daily.md"


class PlistTests(unittest.TestCase):
    def setUp(self):
        self.data = plistlib.loads(PLIST.read_bytes())

    def test_label_and_program(self):
        self.assertEqual(self.data["Label"], "com.mailroom.daily")
        args = self.data["ProgramArguments"]
        self.assertEqual(args[0], "/bin/zsh")
        self.assertTrue(args[1].endswith("/MailArchive/scripts/run_mailroom_daily.sh"))
        self.assertIn("/Users/buck/", args[1])

    def test_schedule_and_keepalive(self):
        self.assertTrue(self.data["RunAtLoad"])
        self.assertFalse(self.data["KeepAlive"])
        interval = self.data["StartCalendarInterval"]
        self.assertEqual(interval["Hour"], 20)
        self.assertEqual(interval["Minute"], 0)

    def test_env_and_logs(self):
        env = self.data["EnvironmentVariables"]
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertIn("/usr/bin", env["PATH"])
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertEqual(env["MAILROOM_KEYCHAIN_ITEM"], "mailroom.icloud.app-password")
        self.assertTrue(self.data["StandardOutPath"].endswith("logs/daily_rag.stdout.log"))
        self.assertTrue(self.data["StandardErrorPath"].endswith("logs/daily_rag.stderr.log"))

    def test_plist_has_no_secret_values(self):
        raw = PLIST.read_text(encoding="utf-8")
        self.assertNotIn("IMAP_APP_PASSWORD", raw)
        self.assertNotIn("@me.com", raw)
        self.assertNotIn("@icloud.com", raw)


class ReadmeTests(unittest.TestCase):
    def test_bootstrap_and_sor(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("launchctl bootstrap gui/$(id -u)", text)
        self.assertIn("mailroom.icloud.app-password", text)
        self.assertIn("last_daily_rag_ok", text)
        self.assertIn("~/MailArchive/.venv/bin/python", text)
        self.assertIn("cannot load sqlite-vec", text)
        self.assertIn("SMB/NFS", text)
        self.assertIn("Promote Mini to SoR", text)
        self.assertIn("cron", text)
        self.assertNotIn("kirkbacon", text)


if __name__ == "__main__":
    unittest.main()
