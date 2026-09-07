#!/usr/bin/env python3
"""Installer contract for mlx generate. No launchctl, no live mlx."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-mlx-generate.sh"


def _run(args: list[str], env: dict[str, str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


class InstallerStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "op"
        self.home.mkdir()
        self.mail = self.home / "MailArchive"
        self.agents = self.home / "Library" / "LaunchAgents"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "MAILARCHIVE": str(self.mail),
                "MAILROOM_LAUNCH_AGENTS": str(self.agents),
                "MAILROOM_INSTALL_SKIP_LAUNCHCTL": "1",
                "MAILROOM_INSTALL_SKIP_VERIFY": "1",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stage_copies_scripts_docs_and_substitutes_home(self) -> None:
        proc = _run([str(INSTALLER), "stage"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PROCESS=mlx_lm.server", proc.stdout)
        for name in (
            "mlx-generate-server.sh",
            "ask_mail.py",
            "mailroom_generate.py",
            "ask_mail_generate_probes.py",
            "install-mlx-generate.sh",
        ):
            dest = self.mail / "scripts" / name
            self.assertTrue(dest.is_file(), name)
        self.assertTrue((self.mail / "docs" / "generate-mlx.md").is_file())
        wrapper = self.mail / "scripts" / "mlx-generate-server.sh"
        self.assertTrue(stat.S_IMODE(wrapper.stat().st_mode) & stat.S_IXUSR)
        plist_path = self.agents / "com.mailroom.mlx-generate.plist"
        data = plistlib.loads(plist_path.read_bytes())
        raw = plist_path.read_text(encoding="utf-8")
        self.assertNotIn("__HOME__", raw)
        self.assertIn(str(self.home), raw)
        self.assertEqual(data["Label"], "com.mailroom.mlx-generate")
        self.assertFalse(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(
            data["ProgramArguments"][1],
            str(self.home / "MailArchive" / "scripts" / "mlx-generate-server.sh"),
        )
        template = (self.mail / "launchd" / "com.mailroom.mlx-generate.plist").read_text(
            encoding="utf-8"
        )
        self.assertIn("__HOME__", template)
        self.assertGreater((self.mail / "scripts" / "ask_mail.py").stat().st_size, 10000)

    def test_install_skips_launchctl_when_asked(self) -> None:
        proc = _run([str(INSTALLER), "install"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skip launchctl", proc.stdout)
        self.assertIn("skip verify", proc.stdout)
        self.assertIn("curl -sS http://127.0.0.1:1234/v1/models", proc.stdout)

    def test_down_does_not_kill(self) -> None:
        proc = _run([str(INSTALLER), "down"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        blob = proc.stdout + proc.stderr
        self.assertIn("bootout", blob)
        self.assertIn("KeepAlive", blob)
        self.assertIn("do not kill", blob.lower())
        self.assertNotIn("kill -9", blob)

    def test_help_lists_verify_and_bootout(self) -> None:
        proc = _run([str(INSTALLER), "help"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = proc.stdout
        self.assertIn("/v1/models", text)
        self.assertIn("bootout", text)
        self.assertIn("NOT kill", text)
        self.assertIn("mlx_lm.server", text)


class StubRefusalTests(unittest.TestCase):
    def test_refuses_stub_ask_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "repo"
            (fake / "scripts").mkdir(parents=True)
            (fake / "launchd").mkdir()
            (fake / "docs").mkdir()
            (fake / "scripts" / "mlx-generate-server.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (fake / "scripts" / "mailroom_generate.py").write_text("x = 1\n", encoding="utf-8")
            (fake / "scripts" / "ask_mail_generate_probes.py").write_text("x = 1\n", encoding="utf-8")
            (fake / "scripts" / "install-mlx-generate.sh").write_text(
                INSTALLER.read_text(encoding="utf-8"), encoding="utf-8"
            )
            (fake / "scripts" / "ask_mail.py").write_text(
                "LOADED_FROM_MCP_PUSH_ASK_JSON\n", encoding="utf-8"
            )
            (fake / "launchd" / "com.mailroom.mlx-generate.plist").write_text(
                (ROOT / "launchd" / "com.mailroom.mlx-generate.plist").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            home = Path(tmp) / "op"
            home.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "MAILARCHIVE": str(home / "MailArchive"),
                    "MAILROOM_LAUNCH_AGENTS": str(home / "Library" / "LaunchAgents"),
                    "MAILROOM_INSTALL_SKIP_LAUNCHCTL": "1",
                    "MAILROOM_INSTALL_SKIP_VERIFY": "1",
                }
            )
            proc = subprocess.run(
                ["bash", str(fake / "scripts" / "install-mlx-generate.sh"), "stage"],
                cwd=str(fake),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("HARD DECK", proc.stderr)
            self.assertIn("stub", proc.stderr.lower())
            dest = home / "MailArchive" / "scripts" / "ask_mail.py"
            self.assertFalse(dest.exists())


class HygieneTests(unittest.TestCase):
    def test_installer_has_no_login_hardcodes(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotIn("EXAMPLE_USER_LOCAL", text)
        self.assertNotIn("@me.com", text)
        self.assertNotIn("lms server start", text)
        self.assertIn("bootout", text)
        self.assertIn("HARD DECK", text)
        self.assertIn("mlx_lm.server", text)
        self.assertIn("bootout", text)
        self.assertIn("KeepAlive", text)

    def test_docs_name_installer_and_bootout(self) -> None:
        docs = (ROOT / "docs" / "generate-mlx.md").read_text(encoding="utf-8")
        self.assertIn("install-mlx-generate.sh", docs)
        self.assertIn("bootout", docs)
        self.assertIn("not `kill`", docs)
        self.assertIn("mlx_lm.server", docs)
        self.assertIn("llmster-headless", docs)
        self.assertIn("fail-open-only", docs)
        self.assertIn("HARD DECK", docs)
        self.assertNotIn("/Users/", docs)


if __name__ == "__main__":
    unittest.main()
