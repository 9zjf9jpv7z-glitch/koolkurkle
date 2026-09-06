#!/usr/bin/env python3
"""Unit tests for Mini daily RAG orchestrator. No network, no Keychain."""

from __future__ import annotations

import hashlib
import os
import stat
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
            "EXAMPLE_USER_LOCAL",
            "@example.invalid",
            "-----BEGIN",
            "ak_live",
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
                self.assertIn("mailroom.imap.app-password", text)


class ShellWrapperTests(unittest.TestCase):
    def test_wrapper_is_zsh_and_skips_if_fresh(self):
        text = (SCRIPTS / "run_mailroom_daily.sh").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/zsh"))
        self.assertIn("mailroom.imap.app-password", text)
        self.assertIn("mailroom.icloud.app-password", text)
        self.assertIn("MAILROOM_KEYCHAIN_ITEM", text)
        self.assertIn("falling back to", text)
        self.assertIn("Live Keychain cutover is not done yet", text)
        self.assertIn("find-generic-password", text)
        self.assertIn("--skip-if-fresh", text)
        self.assertIn("/usr/bin/curl", text)
        self.assertIn(".venv/bin/python", text)
        self.assertNotIn("find-generic-password -w '", text)


NEW_KEYCHAIN = "mailroom.imap.app-password"
LEGACY_KEYCHAIN = "mailroom.icloud.app-password"
NEW_PW = "TEST_PLACEHOLDER_NEW_ITEM"
OLD_PW = "TEST_PLACEHOLDER_OLD_ITEM"
CUSTOM_PW = "TEST_PLACEHOLDER_CUSTOM_ITEM"
PRESET_PW = "TEST_PLACEHOLDER_PRESET_ITEM"
FAKE_SECURITY = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

log = os.environ.get("MAILROOM_FAKE_SECURITY_LOG")
items = os.environ.get("MAILROOM_FAKE_SECURITY_ITEMS", "")
empty = set(os.environ.get("MAILROOM_FAKE_SECURITY_EMPTY", "").split(",")) - {""}
ok = dict(part.split("=", 1) for part in items.split("|") if "=" in part)
args = sys.argv[1:]
svc = args[args.index("-s") + 1] if "-s" in args else ""
if log:
    path = Path(log)
    prev = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(prev + svc + "\n", encoding="utf-8")
if svc in empty:
    sys.exit(0)
if svc in ok:
    sys.stdout.write(ok[svc] + "\n")
    sys.exit(0)
sys.exit(44)
"""
DAILY_STUB = r"""
import hashlib
import os
import sys

pw = os.environ.get("IMAP_APP_PASSWORD", "")
digest = hashlib.sha256(pw.encode()).hexdigest() if pw else "-"
print("password_loaded=%d" % (1 if pw else 0))
print("password_sha256=%s" % digest)
sys.exit(0)
"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class KeychainFallbackTests(unittest.TestCase):
    """Exercise wrapper Keychain read + legacy fallback. No live Keychain."""

    def _run(
        self,
        tmp: Path,
        items: dict[str, str] | None = None,
        empty: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        archive = tmp / "MailArchive"
        scripts = archive / "scripts"
        logs = archive / "logs"
        scripts.mkdir(parents=True)
        logs.mkdir()
        (scripts / "mailroom_daily.py").write_text(DAILY_STUB, encoding="utf-8")
        security = tmp / "fake-security"
        security.write_text(FAKE_SECURITY, encoding="utf-8")
        security.chmod(security.stat().st_mode | stat.S_IEXEC)
        log = tmp / "security.log"
        packed = "|".join("%s=%s" % (k, v) for k, v in (items or {}).items())
        env = {
            **os.environ,
            "MAILARCHIVE": str(archive),
            "MAILARCHIVE_SCRIPTS": str(scripts),
            "MAILARCHIVE_LOGS": str(logs),
            "MAILROOM_SECURITY_BIN": str(security),
            "MAILROOM_APPLE_PY": sys.executable,
            "MAILROOM_FAKE_SECURITY_LOG": str(log),
            "MAILROOM_FAKE_SECURITY_ITEMS": packed,
            "MAILROOM_FAKE_SECURITY_EMPTY": ",".join(empty),
        }
        for key in ("IMAP_APP_PASSWORD", "MAILROOM_KEYCHAIN_ITEM"):
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["/bin/zsh", str(SCRIPTS / "run_mailroom_daily.sh")],
            cwd=str(archive),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        proc.security_log = log.read_text(encoding="utf-8") if log.exists() else ""
        return proc

    def _assert_no_secret_leak(self, proc: subprocess.CompletedProcess[str]) -> None:
        blob = proc.stdout + proc.stderr
        for token in (NEW_PW, OLD_PW, CUSTOM_PW, PRESET_PW):
            self.assertNotIn(token, blob)

    def test_new_name_wins_without_fallback_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={NEW_KEYCHAIN: NEW_PW, LEGACY_KEYCHAIN: OLD_PW},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(NEW_PW), proc.stdout)
        self.assertNotIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [NEW_KEYCHAIN])
        self._assert_no_secret_leak(proc)

    def test_legacy_fallback_warns_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(Path(tmp), items={LEGACY_KEYCHAIN: OLD_PW})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(OLD_PW), proc.stdout)
        self.assertIn(
            "warning: Keychain service %s missing or empty; falling back to %s (one-time). Live Keychain cutover is not done yet."
            % (NEW_KEYCHAIN, LEGACY_KEYCHAIN),
            proc.stderr,
        )
        self.assertEqual(proc.security_log.split(), [NEW_KEYCHAIN, LEGACY_KEYCHAIN])
        self._assert_no_secret_leak(proc)

    def test_empty_new_name_falls_back_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={LEGACY_KEYCHAIN: OLD_PW},
                empty=(NEW_KEYCHAIN,),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(OLD_PW), proc.stdout)
        self.assertIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [NEW_KEYCHAIN, LEGACY_KEYCHAIN])
        self._assert_no_secret_leak(proc)

    def test_neither_item_does_not_fail_the_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(Path(tmp), items={})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=0", proc.stdout)
        self.assertNotIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [NEW_KEYCHAIN, LEGACY_KEYCHAIN])
        self._assert_no_secret_leak(proc)

    def test_override_is_used_without_legacy_fallback(self):
        custom = "mailroom.custom.test-item"
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={custom: CUSTOM_PW, LEGACY_KEYCHAIN: OLD_PW},
                extra_env={"MAILROOM_KEYCHAIN_ITEM": custom},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(CUSTOM_PW), proc.stdout)
        self.assertNotIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [custom])
        self._assert_no_secret_leak(proc)

    def test_override_miss_does_not_use_legacy(self):
        custom = "mailroom.custom.test-item"
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={LEGACY_KEYCHAIN: OLD_PW},
                extra_env={"MAILROOM_KEYCHAIN_ITEM": custom},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=0", proc.stdout)
        self.assertNotIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [custom])
        self._assert_no_secret_leak(proc)

    def test_explicit_new_default_env_still_falls_back(self):
        """Plist sets MAILROOM_KEYCHAIN_ITEM to the new default; fallback still applies."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={LEGACY_KEYCHAIN: OLD_PW},
                extra_env={"MAILROOM_KEYCHAIN_ITEM": NEW_KEYCHAIN},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(OLD_PW), proc.stdout)
        self.assertIn("falling back", proc.stderr)
        self.assertEqual(proc.security_log.split(), [NEW_KEYCHAIN, LEGACY_KEYCHAIN])
        self._assert_no_secret_leak(proc)

    def test_preset_env_skips_keychain(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={NEW_KEYCHAIN: NEW_PW},
                extra_env={"IMAP_APP_PASSWORD": PRESET_PW},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
        self.assertIn("password_sha256=%s" % _sha256(PRESET_PW), proc.stdout)
        self.assertEqual(proc.security_log, "")
        self._assert_no_secret_leak(proc)


if __name__ == "__main__":
    unittest.main()
