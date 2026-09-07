#!/usr/bin/env python3
"""mlx generate LaunchAgent contract. No launchctl."""

from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "launchd" / "com.mailroom.mlx-generate.plist"
WRAPPER = ROOT / "scripts" / "mlx-generate-server.sh"


class MlxGeneratePlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = plistlib.loads(PLIST.read_bytes())

    def test_label_keepalive_not_run_at_load(self) -> None:
        self.assertEqual(self.data["Label"], "com.mailroom.mlx-generate")
        self.assertFalse(self.data["RunAtLoad"])
        self.assertTrue(self.data["KeepAlive"])
        args = self.data["ProgramArguments"]
        self.assertEqual(args[0], "/bin/bash")
        self.assertTrue(args[1].endswith("/MailArchive/scripts/mlx-generate-server.sh"))
        self.assertTrue(args[1].startswith("__HOME__/"))

    def test_no_home_login(self) -> None:
        raw = PLIST.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", raw)
        self.assertIn("__HOME__", raw)
        self.assertIn("mlx_lm.server", raw)


class WrapperTests(unittest.TestCase):
    def test_thinking_off_and_process(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("mlx_lm.server", text)
        self.assertIn("enable_thinking", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn("--port 1234", text)
        self.assertNotIn("lms server start", text)
        self.assertNotIn("/Users/", text)


if __name__ == "__main__":
    unittest.main()
