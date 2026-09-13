#!/usr/bin/env python3
"""Unit tests for Mini daily RAG orchestrator. No network, no Keychain."""

from __future__ import annotations

import hashlib
import inspect
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

import mailroom_copy_db as copy_db  # noqa: E402
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
            self.assertFalse((Path(tmp) / "last_daily_rag_ok.tmp").exists())

    def test_write_ok_stamp_is_atomic_replace(self):
        src = inspect.getsource(daily.write_ok_stamp)
        self.assertIn("replace", src)
        self.assertIn(".tmp", src)


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
            db = archive / "mailroom-copy.sqlite"
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
        for item in items:
            self.assertIn("--db", item.argv, msg=item.script.name)
            db_idx = item.argv.index("--db")
            self.assertEqual(item.argv[db_idx + 1], str(db), msg=item.script.name)
            self.assertEqual(item.extra_env.get("MAILROOM_DB"), str(db), msg=item.script.name)
            self.assertNotIn("mailroom.sqlite", item.argv)
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
                items = daily.build_plan(archive, scripts, archive / "mailroom-copy.sqlite")
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
                # Isolate from repo/scripts children landed in PR-36.
                with patch.object(daily, "search_roots", return_value=[scripts]):
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

    def _copy_db(self, archive: Path) -> Path:
        return archive / "mailroom-copy.sqlite"

    def _run_main(self, archive, scripts, logs, extra=None):
        argv = [
            "--archive",
            str(archive),
            "--scripts",
            str(scripts),
            "--logs",
            str(logs),
            "--db",
            str(self._copy_db(archive)),
        ]
        if extra:
            argv.extend(extra)
        env = {
            "MAILROOM_VENV_PY": str(archive / ".venv" / "bin" / "python"),
            "MAILROOM_APPLE_PY": sys.executable,
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(daily, "check_embed_health"):
                return daily.main(argv)

    def test_success_writes_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            stamp = logs / daily.STAMP_NAME
            rc = self._run_main(archive, scripts, logs, ["--force"])
            self.assertEqual(rc, 0)
            self.assertTrue(stamp.is_file())
            self.assertTrue((logs / daily.IMAP_STAMP).is_file())
            self.assertTrue((logs / daily.BODIES_STAMP).is_file())
            self.assertTrue((logs / daily.EMBED_STAMP).is_file())

    def test_failure_does_not_write_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            _write_executable(
                scripts / "imap_newmail.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(7)\n",
            )
            rc = self._run_main(archive, scripts, logs, ["--force"])
            self.assertEqual(rc, 7)
            self.assertFalse((logs / daily.STAMP_NAME).exists())
            self.assertFalse((logs / daily.IMAP_STAMP).exists())

    def test_classify_warn_still_writes_daily_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            _write_executable(
                scripts / "classify.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(7)\n",
            )
            rc = self._run_main(archive, scripts, logs, ["--force"])
            self.assertEqual(rc, 0)
            self.assertTrue((logs / daily.STAMP_NAME).is_file())
            self.assertTrue((logs / daily.IMAP_STAMP).is_file())
            self.assertTrue((logs / daily.EMBED_STAMP).is_file())

    def test_fresh_stamp_is_quiet_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            stamp = logs / daily.STAMP_NAME
            daily.write_ok_stamp(stamp)
            env = {
                "MAILROOM_VENV_PY": str(archive / ".venv" / "bin" / "python"),
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
                                "--db",
                                str(self._copy_db(archive)),
                                "--skip-if-fresh",
                            ]
                        )
            self.assertEqual(rc, 0)
            plan.assert_not_called()
            stdout.write.assert_not_called()

    def test_dry_run_does_not_write_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            rc = self._run_main(archive, scripts, logs, ["--dry-run"])
            self.assertEqual(rc, 0)
            self.assertFalse((logs / daily.STAMP_NAME).exists())

    def test_resume_skips_successful_imap_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            daily.write_ok_stamp(logs / daily.IMAP_STAMP)
            _write_executable(
                scripts / "imap_newmail.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(9)\n",
            )
            rc = self._run_main(archive, scripts, logs, ["--force"])
            self.assertEqual(rc, 0)
            self.assertTrue((logs / daily.STAMP_NAME).is_file())
            self.assertTrue((logs / daily.BODIES_STAMP).is_file())
            self.assertTrue((logs / daily.EMBED_STAMP).is_file())

    def test_stale_watermarks_are_redone(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, scripts, logs, _venv_py = self._layout(Path(tmp))
            now = 5_000_000.0
            stale = now - daily.CATCH_UP_MAX_AGE_SEC - 60
            daily.write_ok_stamp(logs / daily.STAMP_NAME, now=stale)
            daily.write_ok_stamp(logs / daily.IMAP_STAMP, now=stale)
            _write_executable(
                scripts / "imap_newmail.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(9)\n",
            )
            rc = self._run_main(archive, scripts, logs, ["--force"])
            self.assertEqual(rc, 9)
            self.assertLess(
                (logs / daily.STAMP_NAME).stat().st_mtime,
                now - 1000,
            )


class PhaseWatermarkTests(unittest.TestCase):
    def test_missing_phase_is_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase = Path(tmp) / "last_imap_ok"
            daily_stamp = Path(tmp) / "last_daily_rag_ok"
            self.assertFalse(
                daily.phase_done_this_cycle(phase, daily_stamp, now=1_000_000)
            )

    def test_fresh_phase_without_daily_stamp_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase = Path(tmp) / "last_imap_ok"
            daily_stamp = Path(tmp) / "last_daily_rag_ok"
            now = 2_000_000.0
            daily.write_ok_stamp(phase, now=now - 60)
            self.assertTrue(daily.phase_done_this_cycle(phase, daily_stamp, now=now))

    def test_stale_phase_is_redone(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase = Path(tmp) / "last_imap_ok"
            daily_stamp = Path(tmp) / "last_daily_rag_ok"
            now = 3_000_000.0
            daily.write_ok_stamp(phase, now=now - daily.CATCH_UP_MAX_AGE_SEC - 10)
            self.assertFalse(daily.phase_done_this_cycle(phase, daily_stamp, now=now))

    def test_phase_older_than_daily_stamp_is_redone(self):
        with tempfile.TemporaryDirectory() as tmp:
            phase = Path(tmp) / "last_imap_ok"
            daily_stamp = Path(tmp) / "last_daily_rag_ok"
            now = 4_000_000.0
            daily.write_ok_stamp(phase, now=now - 120)
            daily.write_ok_stamp(daily_stamp, now=now - 30)
            self.assertFalse(daily.phase_done_this_cycle(phase, daily_stamp, now=now))


class CopyDbGuardTests(unittest.TestCase):
    """Interface proof + negative smoke. No live IMAP."""

    def test_allowlist_accepts_copy_basenames(self):
        self.assertTrue(daily.allowed_copy_db(Path("/tmp/mailroom-copy.sqlite")))
        self.assertTrue(
            daily.allowed_copy_db(Path("/tmp/mailroom-daily-copy.sqlite"))
        )
        self.assertEqual(
            daily.resolve_driver_db("/tmp/mailroom-copy.sqlite", Path("/tmp")).name,
            "mailroom-copy.sqlite",
        )
        self.assertEqual(
            daily.resolve_driver_db(
                "/tmp/mailroom-daily-copy.sqlite", Path("/tmp")
            ).name,
            "mailroom-daily-copy.sqlite",
        )

    def test_interface_proof_refuses_sor_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "mailroom_daily.py"),
                    "--archive",
                    tmp,
                    "--db",
                    str(db),
                    "--print-plan",
                    "--allow-missing",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "MAILROOM_DB": str(db)},
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("db_mode=refused", proc.stderr)
        self.assertIn("mailroom.sqlite", proc.stderr)
        self.assertIn("allowlist", proc.stderr)
        self.assertNotIn("imap_newmail", proc.stdout)
        self.assertNotIn("db_mode=copy", proc.stderr)

    def test_interface_proof_accepts_copy_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("mailroom-copy.sqlite", "mailroom-daily-copy.sqlite"):
                db = Path(tmp) / name
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "mailroom_daily.py"),
                        "--archive",
                        tmp,
                        "--scripts",
                        tmp,
                        "--logs",
                        tmp,
                        "--db",
                        str(db),
                        "--print-plan",
                        "--allow-missing",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, "MAILROOM_DB": str(db)},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("db_mode=copy", proc.stderr)
                self.assertNotIn("db_mode=refused", proc.stderr)

    def test_unset_mailroom_db_refuses(self):
        env = {k: v for k, v in os.environ.items() if k != "MAILROOM_DB"}
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "mailroom_daily.py"),
                    "--archive",
                    tmp,
                    "--print-plan",
                    "--allow-missing",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("db_mode=refused", proc.stderr)
        self.assertIn("unset", proc.stderr)
        self.assertIn("explicit copy path", proc.stderr)

    def test_negative_smoke_fails_if_guard_allows_sor_or_missing_allowlist(self):
        """FAIL if the guard would allow mailroom.sqlite or has no allowlist."""
        self.assertTrue(hasattr(daily, "COPY_DB_BASENAMES"))
        self.assertIn("mailroom-copy.sqlite", daily.COPY_DB_BASENAMES)
        self.assertIn("mailroom-daily-copy.sqlite", daily.COPY_DB_BASENAMES)
        self.assertNotIn("mailroom.sqlite", daily.COPY_DB_BASENAMES)
        self.assertFalse(daily.allowed_copy_db(Path("/x/mailroom.sqlite")))
        self.assertFalse(daily.allowed_copy_db(Path("/x/other.sqlite")))
        with self.assertRaises(daily.DailyRefuse) as ctx:
            daily.resolve_driver_db("mailroom.sqlite", Path("/tmp"))
        self.assertIn("mailroom.sqlite", str(ctx.exception))
        with patch.dict(os.environ, {"MAILROOM_DB": ""}, clear=False):
            with self.assertRaises(daily.DailyRefuse):
                daily.resolve_driver_db(None, Path("/tmp"))
        src = inspect.getsource(daily.main)
        self.assertIn("resolve_driver_db", src)
        self.assertIn("DailyRefuse", src)
        self.assertIn("emit_db_mode", src)
        with patch.dict(os.environ, {"MAILROOM_DB": ""}, clear=False):
            self.assertIsNone(daily.default_db_path(Path("/tmp")))

    def test_refuse_is_hard_fail_not_fail_open(self):
        src = inspect.getsource(daily.main)
        self.assertIn('emit_db_mode("refused")', src)
        self.assertIn("return 2", src)
        self.assertNotIn("fail_open", src)


CHILD_PROBE = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("MAILROOM_COPY_DB_DIR", str(Path(__file__).resolve().parent)))
import mailroom_copy_db as cdb

path = cdb.resolve_from_argv(sys.argv[1:])
log = os.environ.get("MAILROOM_CHILD_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(
            "script=%s opened_db=%s argv_has_db=%s env=%s\n"
            % (
                Path(sys.argv[0]).name,
                path,
                "--db" in sys.argv,
                os.environ.get("MAILROOM_DB", ""),
            )
        )
print("opened_db=%s" % path)
sys.exit(0)
"""


class ChildDbHonorTests(unittest.TestCase):
    """Children honor --db / MAILROOM_DB. No live IMAP."""

    def _touch_probe_scripts(self, folder: Path, helper_dir: Path) -> Path:
        log = folder.parent / "child.log"
        for name in (
            "imap_newmail.py",
            "imap_tombstone.py",
            "imap_fetch_bodies_fts.py",
            "classify.py",
            "notify_bills.py",
            "embed_backfill.py",
        ):
            _write_executable(folder / name, CHILD_PROBE)
        return log

    def test_negative_smoke_fails_if_child_would_open_sor_when_copy(self):
        """FAIL if a daily child would open mailroom.sqlite while MAILROOM_DB is a copy."""
        src = inspect.getsource(daily.build_plan)
        self.assertIn('extra.extend(("--db", str(db)))', src)
        self.assertIn('extra_env["MAILROOM_DB"] = str(db)', src)
        embed_only = src.find('if step.name == "embed"')
        db_wire = src.find('extra.extend(("--db", str(db)))')
        self.assertGreater(embed_only, 0)
        self.assertLess(
            db_wire,
            embed_only,
            msg="--db must be wired for every daily child, not only embed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "MailArchive"
            scripts = archive / "scripts"
            scripts.mkdir(parents=True)
            for name in (
                "imap_newmail.py",
                "imap_tombstone.py",
                "imap_fetch_bodies_fts.py",
                "classify.py",
                "notify_bills.py",
                "embed_backfill.py",
            ):
                (scripts / name).write_text("# fake\n", encoding="utf-8")
            venv_py = archive / ".venv" / "bin" / "python"
            venv_py.parent.mkdir(parents=True)
            _write_executable(venv_py, "#!/usr/bin/env python3\n")
            copy = archive / "mailroom-copy.sqlite"
            with patch.dict(os.environ, {"MAILROOM_VENV_PY": str(venv_py)}, clear=False):
                items = daily.build_plan(archive, scripts, copy)
        self.assertTrue(items)
        for item in items:
            self.assertFalse(
                copy_db.child_would_open_sor(item.argv, item.extra_env),
                msg="%s would open SoR when MAILROOM_DB is a copy" % item.script.name,
            )
            self.assertIn("--db", item.argv)
            self.assertEqual(item.argv[item.argv.index("--db") + 1], str(copy))
            self.assertEqual(item.extra_env.get("MAILROOM_DB"), str(copy))
            self.assertNotIn("mailroom.sqlite", item.argv)
            self.assertNotEqual(Path(item.extra_env["MAILROOM_DB"]).name, "mailroom.sqlite")
        self.assertTrue(
            copy_db.child_would_open_sor([], {}),
            msg="unset child path must be treated as SoR-open / refuse",
        )
        self.assertTrue(
            copy_db.child_would_open_sor(
                ["--db", "/tmp/mailroom.sqlite"],
                {"MAILROOM_DB": str(copy)},
            )
        )

    def test_interface_proof_copy_db_reaches_child_argv_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "MailArchive"
            scripts = archive / "scripts"
            logs = archive / "logs"
            scripts.mkdir(parents=True)
            logs.mkdir()
            venv_py = archive / ".venv" / "bin" / "python"
            venv_py.parent.mkdir(parents=True)
            _write_executable(
                venv_py,
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n",
            )
            child_log = self._touch_probe_scripts(scripts, SCRIPTS)
            copy = archive / "mailroom-copy.sqlite"
            env = {
                "MAILROOM_VENV_PY": str(venv_py),
                "MAILROOM_APPLE_PY": sys.executable,
                "MAILROOM_COPY_DB_DIR": str(SCRIPTS),
                "MAILROOM_CHILD_LOG": str(child_log),
                "MAILROOM_DB": str(copy),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(daily, "check_embed_health"):
                    rc = daily.main(
                        [
                            "--archive",
                            str(archive),
                            "--scripts",
                            str(scripts),
                            "--logs",
                            str(logs),
                            "--db",
                            str(copy),
                            "--force",
                        ]
                    )
            self.assertEqual(rc, 0)
            text = child_log.read_text(encoding="utf-8")
            for name in (
                "imap_newmail.py",
                "imap_tombstone.py",
                "imap_fetch_bodies_fts.py",
                "classify.py",
                "notify_bills.py",
                "embed_backfill.py",
            ):
                self.assertIn("script=%s" % name, text)
                self.assertIn("opened_db=%s" % copy, text)
            self.assertIn("argv_has_db=True", text)
            self.assertIn("env=%s" % copy, text)
            self.assertNotIn("mailroom.sqlite", text)

    def test_interface_proof_helper_refuses_sor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "mailroom_copy_db.py"), "--db", str(db)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("db_mode=refused", proc.stderr)
        self.assertIn("mailroom.sqlite", proc.stderr)
        self.assertNotIn("db_mode=copy", proc.stderr)

    def test_interface_proof_helper_accepts_copy_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom-daily-copy.sqlite"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "mailroom_copy_db.py")],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "MAILROOM_DB": str(db)},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("db_mode=copy", proc.stderr)
        self.assertIn(str(db), proc.stdout)


class FlockTests(unittest.TestCase):
    def test_acquire_and_second_holder_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.daily.lock"
            held = daily.acquire_daily_lock(lock)
            try:
                with self.assertRaises(daily.DailyLockHeld) as ctx:
                    daily.acquire_daily_lock(lock)
                self.assertIn("mailroom.daily.lock", str(ctx.exception))
                self.assertIn("double-run", str(ctx.exception))
            finally:
                daily.release_daily_lock(held)
            held2 = daily.acquire_daily_lock(lock)
            daily.release_daily_lock(held2)

    def test_held_lock_skips_main_without_imap(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "MailArchive"
            archive.mkdir()
            lock = archive / daily.LOCK_NAME
            held = daily.acquire_daily_lock(lock)
            try:
                with patch.object(daily, "build_plan") as plan:
                    rc = daily.main(
                        [
                            "--archive",
                            str(archive),
                            "--db",
                            str(archive / "mailroom-copy.sqlite"),
                            "--force",
                        ]
                    )
                self.assertEqual(rc, 0)
                plan.assert_not_called()
            finally:
                daily.release_daily_lock(held)


class EmbedHealthTests(unittest.TestCase):
    def test_health_ok(self):
        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(daily.urllib.request, "urlopen", return_value=_Resp()):
            daily.check_embed_health()

    def test_health_refuses_when_down(self):
        with patch.object(
            daily.urllib.request,
            "urlopen",
            side_effect=daily.urllib.error.URLError("down"),
        ):
            with self.assertRaises(daily.DailyError) as ctx:
                daily.check_embed_health()
        self.assertIn("/api/tags", str(ctx.exception))

    def test_embed_health_url_uses_ollama_host(self):
        with patch.dict(
            os.environ, {"OLLAMA_HOST": "http://127.0.0.1:11434"}, clear=False
        ):
            self.assertEqual(
                daily.embed_health_url(),
                "http://127.0.0.1:11434/api/tags",
            )


class SourceHygieneTests(unittest.TestCase):
    def test_no_secrets_in_daily_files(self):
        files = [
            ROOT / "scripts" / "mailroom_daily.py",
            ROOT / "scripts" / "run_mailroom_daily.sh",
            ROOT / "scripts" / "mailroom_copy_db.py",
            ROOT / "scripts" / "imap_newmail.py",
            ROOT / "scripts" / "imap_tombstone.py",
            ROOT / "scripts" / "imap_fetch_bodies_fts.py",
            ROOT / "scripts" / "classify.py",
            ROOT / "scripts" / "notify_bills.py",
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
            self.assertNotIn("com.baconhill.mailroom-daily", text)

    def test_daily_driver_does_not_start_generate(self):
        text = (SCRIPTS / "mailroom_daily.py").read_text(encoding="utf-8")
        self.assertNotIn("ask_mail.py", text)
        self.assertNotIn("lms load", text)
        self.assertNotIn("LM Studio.app", text)
        self.assertNotIn("open -a", text)
        self.assertNotIn("35B", text)
        self.assertIn("/api/tags", text)


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
        self.assertIn("mailroom-copy.sqlite", text)
        self.assertIn("mailroom-daily-copy.sqlite", text)
        self.assertIn("db_mode=refused", text)
        self.assertIn("db_mode=copy", text)
        self.assertIn("mailroom.daily.lock", text)
        self.assertNotIn("find-generic-password -w '", text)
        self.assertNotIn("com.baconhill.mailroom-daily", text)


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
            "MAILROOM_DB": str(archive / "mailroom-copy.sqlite"),
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

    def test_wrapper_refuses_sor_basename_before_keychain(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={NEW_KEYCHAIN: NEW_PW},
                extra_env={
                    "MAILROOM_DB": str(Path(tmp) / "MailArchive" / "mailroom.sqlite"),
                },
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("db_mode=refused", proc.stderr)
        self.assertIn("mailroom.sqlite", proc.stderr)
        self.assertNotIn("password_loaded", proc.stdout)
        self.assertEqual(proc.security_log, "")
        self._assert_no_secret_leak(proc)

    def test_wrapper_refuses_unset_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={NEW_KEYCHAIN: NEW_PW},
                extra_env={"MAILROOM_DB": ""},
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("db_mode=refused", proc.stderr)
        self.assertIn("unset", proc.stderr)
        self.assertNotIn("password_loaded", proc.stdout)
        self.assertEqual(proc.security_log, "")
        self._assert_no_secret_leak(proc)

    def test_wrapper_accepts_daily_copy_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                Path(tmp),
                items={NEW_KEYCHAIN: NEW_PW},
                extra_env={
                    "MAILROOM_DB": str(
                        Path(tmp) / "MailArchive" / "mailroom-daily-copy.sqlite"
                    ),
                },
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("db_mode=copy", proc.stderr)
        self.assertIn("password_loaded=1", proc.stdout)
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
