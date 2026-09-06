#!/usr/bin/env python3
"""Unit tests for Mini daily RAG orchestrator. No network, no Keychain."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mailroom_daily as daily  # noqa: E402


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class StampTests(unittest.TestCase):
    def test_missing_stamp_should_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last_daily_rag_ok"
            self.assertIsNone(daily.stamp_age_seconds(stamp, now=1_000_000))
            self.assertTrue(daily.should_run_pipeline(stamp, now=1_000_000))

    def test_fresh_stamp_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last_daily_rag_ok"
            now = 2_000_000.0
            daily.write_ok_stamp(stamp, now=now - 3600)
            self.assertFalse(daily.should_run_pipeline(stamp, now=now))

    def test_stale_stamp_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last_daily_rag_ok"
            now = 3_000_000.0
            daily.write_ok_stamp(stamp, now=now - daily.CATCH_UP_MAX_AGE_SEC - 10)
            self.assertTrue(daily.should_run_pipeline(stamp, now=now))

    def test_almost_24h_runs_due_to_slop(self):
        """20:00 calendar must not skip when last night's stamp is 23h 50m old."""
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last_daily_rag_ok"
            now = 4_000_000.0
            age = daily.CATCH_UP_MAX_AGE_SEC - 10 * 60
            daily.write_ok_stamp(stamp, now=now - age)
            self.assertTrue(daily.should_run_pipeline(stamp, now=now))

    def test_write_ok_stamp_is_utc_iso(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last_daily_rag_ok"
            daily.write_ok_stamp(stamp, now=0)
            text = stamp.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("1970-01-01T00:00:00Z"))


class EmbedPythonTests(unittest.TestCase):
    def test_refuses_apple_python(self):
        with self.assertRaises(daily.DailyError) as ctx:
            daily.refuse_apple_python_for_embed(Path("/usr/bin/python3"))
        self.assertIn("sqlite-vec", str(ctx.exception))
        self.assertIn(".venv", str(ctx.exception))


class PlanTests(unittest.TestCase):
    def _touch_scripts(self, folder: Path) -> None:
        for name in (
            "imap_newmail.py",
            "imap_tombstone.py",
            "imap_fetch_bodies_fts.py",
            "classify.py",
            "notify_bills.py",
            "embed_backfill.py",
        ):
            (folder / name).write_text("# fake %s\n" % name, encoding="utf-8")

    def test_build_plan_wires_apple_curl_then_unsets_for_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "MailArchive"
            scripts = archive / "scripts"
            scripts.mkdir(parents=True)
            self._touch_scripts(scripts)
            venv_py = archive / ".venv" / "bin" / "python"
            venv_py.parent.mkdir(parents=True)
            _write_executable(venv_py, "#!/usr/bin/env python3\n")
            db = archive / "mailroom.sqlite"
            with patch.dict(os.environ, {"MAILROOM_VENV_PY": str(venv_py)}, clear=False):
                items = daily.build_plan(archive, scripts, db)
        names = [(i.step, i.script.name) for i in items]
        self.assertEqual(
            names,
            [
                ("headers", "imap_newmail.py"),
                ("headers", "imap_tombstone.py"),
                ("bodies-fts", "imap_fetch_bodies_fts.py"),
                ("classify", "classify.py"),
                ("bills", "notify_bills.py"),
                ("embed", "embed_backfill.py"),
            ],
        )
        header = items[0]
        self.assertEqual(header.extra_env.get("CURL_BIN"), "/usr/bin/curl")
        body = [i for i in items if i.step == "bodies-fts"][0]
        self.assertIn("CURL_BIN", body.unset_env)
        embed = items[-1]
        self.assertIn("--db", embed.argv)
        self.assertIn(str(db), embed.argv)
        self.assertIn("--skip-auth", embed.argv)
        self.assertIn("--quote-strip", embed.argv)
        self.assertIn("--lock", embed.argv)
        self.assertEqual(embed.python, venv_py)

    def test_prefers_fts_script_over_bodies_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            scripts = archive / "scripts"
            scripts.mkdir()
            self._touch_scripts(scripts)
            (scripts / "imap_fetch_bodies.py").write_text("# older\n", encoding="utf-8")
            venv_py = archive / "venvpy"
            _write_executable(venv_py, "#!/usr/bin/env python3\n")
            with patch.dict(os.environ, {"MAILROOM_VENV_PY": str(venv_py)}, clear=False):
                items = daily.build_plan(archive, scripts, archive / "mailroom.sqlite")
        body = [i for i in items if i.step == "bodies-fts"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0].script.name, "imap_fetch_bodies_fts.py")

    def test_missing_scripts_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)
            scripts = archive / "scripts"
            scripts.mkdir()
            venv_py = archive / "venvpy"
            _write_executable(venv_py, "#!/usr/bin/env python3\n")
            with patch.dict(os.environ, {"MAILROOM_VENV_PY": str(venv_py)}, clear=False):
                with self.assertRaises(daily.DailyError) as ctx:
                    daily.build_plan(archive, scripts, archive / "db.sqlite")
        self.assertIn("missing required", str(ctx.exception))


class MainChainTests(unittest.TestCase):
    def _layout(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        archive = tmp / "MailArchive"
        scripts = archive / "scripts"
        logs = archive / "logs"
        scripts.mkdir(parents=True)
        logs.mkdir()
        venv_py = archive / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        _write_executable(venv_py, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        ok = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
        for name in (
            "imap_newmail.py",
            "imap_tombstone.py",
            "imap_fetch_bodies_fts.py",
            "classify.py",
            "notify_bills.py",
            "embed_backfill.py",
        ):
            _write_executable(scripts / name, ok)
        return archive, scripts, logs, venv_py

    def test_success_writes_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, venv_py = self._layout(Path(tmp))
            stamp = logs / daily.STAMP_NAME
            env = {
                "MAILROOM_VENV_PY": str(venv_py),
                "MAILROOM_APPLE_PY": sys.executable,
            }
            with patch.dict(os.environ, env, clear=False):
                rc = daily.main(
                    [
                        "--archive",
                        str(archive),
                        "--scripts",
                        str(scripts),
                        "--logs",
                        str(logs),
                        "--force",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(stamp.is_file())

    def test_failure_does_not_write_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, venv_py = self._layout(Path(tmp))
            _write_executable(
                scripts / "classify.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(7)\n",
            )
            env = {
                "MAILROOM_VENV_PY": str(venv_py),
                "MAILROOM_APPLE_PY": sys.executable,
            }
            with patch.dict(os.environ, env, clear=False):
                rc = daily.main(
                    [
                        "--archive",
                        str(archive),
                        "--scripts",
                        str(scripts),
                        "--logs",
                        str(logs),
                        "--force",
                    ]
                )
            self.assertEqual(rc, 7)
            self.assertFalse((logs / daily.STAMP_NAME).exists())

    def test_fresh_stamp_is_quiet_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, venv_py = self._layout(Path(tmp))
            stamp = logs / daily.STAMP_NAME
            daily.write_ok_stamp(stamp)
            env = {
                "MAILROOM_VENV_PY": str(venv_py),
                "MAILROOM_APPLE_PY": sys.executable,
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(daily, "build_plan") as plan:
                    with patch("sys.stdout") as stdout:
                        rc = daily.main(
                            [
                                "--archive",
                                str(archive),
                                "--scripts",
                                str(scripts),
                                "--logs",
                                str(logs),
                                "--skip-if-fresh",
                            ]
                        )
            self.assertEqual(rc, 0)
            plan.assert_not_called()
            stdout.write.assert_not_called()

    def test_dry_run_does_not_write_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, venv_py = self._layout(Path(tmp))
            env = {
                "MAILROOM_VENV_PY": str(venv_py),
                "MAILROOM_APPLE_PY": sys.executable,
            }
            with patch.dict(os.environ, env, clear=False):
                rc = daily.main(
                    [
                        "--archive",
                        str(archive),
                        "--scripts",
                        str(scripts),
                        "--logs",
                        str(logs),
                        "--dry-run",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertFalse((logs / daily.STAMP_NAME).exists())


class SourceHygieneTests(unittest.TestCase):
    def test_no_secrets_in_daily_files(self):
        files = [
            ROOT / "scripts" / "mailroom_daily.py",
            ROOT / "scripts" / "run_mailroom_daily.sh",
            ROOT / "scripts" / "ask_mail.py",
            ROOT / "scripts" / "README.mailroom-daily.md",
            ROOT / "launchd" / "com.mailroom.daily.plist",
            ROOT / "README.md",
        ]
        forbidden = (
            "kirkbacon",
            "@me.com",
            "-----BEGIN",
            "ak_live",
            "/Users/buck",
            "/Users/Buck",
        )
        named = {
            "run_mailroom_daily.sh",
            "README.mailroom-daily.md",
            "com.mailroom.daily.plist",
            "README.md",
        }
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, msg=path.name)
            if path.name in named:
                self.assertIn("mailroom.icloud.app-password", text)


class ShellWrapperTests(unittest.TestCase):
    def test_wrapper_is_zsh_and_skips_if_fresh(self):
        text = (SCRIPTS / "run_mailroom_daily.sh").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/zsh"))
        self.assertIn("mailroom.icloud.app-password", text)
        self.assertIn("find-generic-password", text)
        self.assertIn("--skip-if-fresh", text)
        self.assertIn("/usr/bin/curl", text)
        self.assertIn(".venv/bin/python", text)
        self.assertNotIn("find-generic-password -w '", text)


if __name__ == "__main__":
    unittest.main()
