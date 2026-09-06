#!/usr/bin/env python3
"""SoR health pack tests. Fixture DB / mocks — no live SoR, no Ollama."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sor_health_pack as hp  # noqa: E402

SECRET_BODY = "BODY_MUST_NOT_APPEAR_IN_HEALTH_OUTPUT"
FORBIDDEN = (
    "kirkbacon",
    "@me.com",
    "-----BEGIN",
    "ak_live",
    "/Users/buck",
    "/Users/Buck",
)


def _fixture(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages (
          id TEXT PRIMARY KEY,
          date_utc TEXT,
          from_addr TEXT,
          subject TEXT,
          snippet TEXT,
          lane TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
          id UNINDEXED, subject, body, from_addr, tokenize='porter unicode61'
        );
        CREATE TABLE embedding_meta (
          message_id TEXT NOT NULL,
          model TEXT NOT NULL,
          model_version TEXT NOT NULL DEFAULT 'v1',
          created_at TEXT NOT NULL,
          text_hash TEXT NOT NULL,
          char_count INTEGER NOT NULL DEFAULT 0,
          dims INTEGER NOT NULL DEFAULT 1024,
          PRIMARY KEY (message_id, model, model_version)
        );
        CREATE TABLE message_embeddings (
          message_id TEXT PRIMARY KEY,
          embedding BLOB
        );
        """
    )
    rows = (
        ("m1", "SDGE bill September", "Please pay your SDGE bill " + SECRET_BODY, "money"),
        ("m2", "Note from Caddell", "Hello from Caddell " + SECRET_BODY, "people"),
        ("m3", "Harbor newsletter", "waves " + SECRET_BODY, "inbox"),
        ("m4", "Invoice due Friday", "bill invoice " + SECRET_BODY, "money"),
    )
    for mid, subject, body, lane in rows:
        conn.execute(
            "INSERT INTO messages(id, date_utc, from_addr, subject, snippet, lane) "
            "VALUES (?, '2026-09-01T00:00:00Z', 'a@example.com', ?, ?, ?)",
            (mid, subject, subject, lane),
        )
        conn.execute(
            "INSERT INTO messages_fts(id, subject, body, from_addr) VALUES (?, ?, ?, ?)",
            (mid, subject, body, "a@example.com"),
        )
    for mid in ("m1", "m2"):
        conn.execute(
            "INSERT INTO embedding_meta("
            "message_id, model, model_version, created_at, text_hash, char_count, dims"
            ") VALUES (?, 'qwen3-embedding-8b', 'v1', '2026-09-01T00:00:00Z', 'h', 10, 1024)",
            (mid,),
        )
        conn.execute(
            "INSERT INTO message_embeddings(message_id, embedding) VALUES (?, X'00')",
            (mid,),
        )
    conn.commit()
    conn.close()


def _run(
    db: Path,
    *,
    skip_hybrid: bool = True,
    retrieve_fn=None,
    embed_probe_fn=None,
    writer_scan_fn=None,
    backups_path: Path | None = None,
    ollama_up: bool | None = False,
) -> hp.HealthReport:
    return hp.run_health(
        db,
        skip_hybrid=skip_hybrid,
        skip_writers=writer_scan_fn is None,
        retrieve_fn=retrieve_fn,
        embed_probe_fn=embed_probe_fn,
        writer_scan_fn=writer_scan_fn,
        backups_path=backups_path,
        ollama_up=ollama_up,
    )


class PathTests(unittest.TestCase):
    def test_default_db_uses_mailroom_env(self):
        with mock.patch.dict(
            os.environ, {"MAILROOM_DB": "~/custom/mailroom.sqlite"}, clear=False
        ):
            path = hp.default_db_path()
        self.assertEqual(path, Path("~/custom/mailroom.sqlite").expanduser())
        self.assertNotIn("/Users/buck", str(path))
        self.assertNotIn("/Users/Buck", str(path))

    def test_default_db_uses_path_home(self):
        fake_home = Path("/tmp/testhome-sor-health")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAILROOM_DB", None)
            with mock.patch.object(hp.Path, "home", return_value=fake_home):
                path = hp.default_db_path()
        self.assertEqual(path, fake_home / "MailArchive" / "mailroom.sqlite")

    def test_empty_mailroom_db_falls_back_to_home(self):
        fake_home = Path("/tmp/testhome-empty-env")
        with mock.patch.dict(os.environ, {"MAILROOM_DB": "  "}, clear=False):
            with mock.patch.object(hp.Path, "home", return_value=fake_home):
                path = hp.default_db_path()
        self.assertEqual(path, fake_home / "MailArchive" / "mailroom.sqlite")

    def test_backups_use_path_home(self):
        fake_home = Path("/tmp/testhome-backups")
        with mock.patch.object(hp.Path, "home", return_value=fake_home):
            path = hp.default_backups_path()
        self.assertEqual(path, fake_home / "MailArchive" / "backups")


class FixtureHealthTests(unittest.TestCase):
    def test_integrity_ok_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = _run(db)
        self.assertTrue(report.opened)
        self.assertTrue(report.integrity_ok)
        self.assertEqual(report.integrity.lower(), "ok")
        self.assertEqual(report.messages, 4)
        self.assertEqual(report.embeddings, 2)
        self.assertEqual(report.embedding_meta, 2)
        self.assertEqual(report.coverage_gap, 2)
        self.assertTrue(report.fts_present)
        self.assertEqual(report.exit_code(), 0)
        self.assertTrue(any("coverage gap" in w for w in report.warnings))

    def test_fts_subjects_not_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = _run(db)
            text = hp.format_report(report)
        bill = next(item for item in report.fts if item.query == "bill")
        people = next(item for item in report.fts if item.query == "Caddell")
        self.assertGreaterEqual(bill.hits, 1)
        self.assertGreaterEqual(people.hits, 1)
        self.assertTrue(any("bill" in s.lower() for s in bill.subjects))
        self.assertTrue(any("Caddell" in s for s in people.subjects))
        self.assertNotIn(SECRET_BODY, text)
        self.assertNotIn(SECRET_BODY, json.dumps(hp.report_to_json(report)))

    def test_no_vec_is_warning_not_hard_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty.sqlite"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, subject TEXT)")
            conn.execute("INSERT INTO messages VALUES ('m1', 'hello')")
            conn.commit()
            conn.close()
            report = _run(db)
        self.assertTrue(report.integrity_ok)
        self.assertEqual(report.exit_code(), 0)
        self.assertTrue(any("no vec" in w for w in report.warnings))
        self.assertFalse(report.fts_present)

    def test_missing_db_is_hard_fail(self):
        report = hp.run_health(
            Path("/no/such/mailroom.sqlite"),
            skip_hybrid=True,
            skip_writers=True,
        )
        self.assertFalse(report.opened)
        self.assertEqual(report.exit_code(), 2)
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = hp.main(
                ["--db", "/no/such/mailroom.sqlite", "--skip-hybrid", "--skip-writers"]
            )
        self.assertEqual(rc, 2)

    def test_integrity_fail_exits_nonzero(self):
        class _Rows:
            def fetchall(self):
                return [("*** in database main ***",)]

        class _Conn:
            def execute(self, sql):
                self.sql = sql
                return _Rows()

        ok, raw = hp.check_integrity(_Conn())
        self.assertFalse(ok)
        self.assertIn("database", raw)
        report = hp.HealthReport(
            db="x",
            opened=True,
            integrity_ok=False,
            integrity="not ok",
            messages=0,
            embeddings=0,
            embedding_meta=0,
            coverage_gap=0,
            fts_present=False,
        )
        report.errors.append("integrity_check not ok")
        self.assertEqual(report.exit_code(), 1)

    def test_backups_presence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            missing = Path(tmp) / "backups"
            present = Path(tmp) / "have-backups"
            present.mkdir()
            miss = _run(db, backups_path=missing)
            have = _run(db, backups_path=present)
        self.assertFalse(miss.backups_present)
        self.assertTrue(any("backups path missing" in w for w in miss.warnings))
        self.assertEqual(miss.exit_code(), 0)
        self.assertTrue(have.backups_present)


class HybridSmokeTests(unittest.TestCase):
    def test_reports_real_vec_rank(self):
        def retrieve(query, **_kwargs):
            return [
                {
                    "message_id": "m1",
                    "subject": "SDGE bill September",
                    "vec_rank": 1,
                    "fts_rank": 2,
                    "snippet": SECRET_BODY,
                },
                {
                    "message_id": "m2",
                    "subject": "Note from Caddell",
                    "vec_rank": 1000,
                    "fts_rank": 1,
                    "snippet": SECRET_BODY,
                },
            ]

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = hp.run_health(
                db,
                skip_writers=True,
                retrieve_fn=retrieve,
                embed_probe_fn=lambda _q: ([0.1] * 8, None),
                ollama_up=True,
            )
        self.assertEqual(report.exit_code(), 0)
        self.assertTrue(report.hybrid)
        self.assertGreaterEqual(report.hybrid[0].vec_real, 1)
        text = hp.format_report(report)
        self.assertNotIn(SECRET_BODY, text)

    def test_fail_open_when_embed_down(self):
        def retrieve(query, **_kwargs):
            return [
                {
                    "message_id": "m1",
                    "subject": "SDGE bill September",
                    "vec_rank": 1000,
                    "fts_rank": 1,
                }
            ]

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = hp.run_health(
                db,
                skip_writers=True,
                retrieve_fn=retrieve,
                ollama_up=False,
            )
        self.assertEqual(report.exit_code(), 0)
        self.assertTrue(all(item.fail_open for item in report.hybrid))
        self.assertTrue(any("fail-open" in w.lower() for w in report.warnings))

    def test_all_missing_vec_rank_is_warning(self):
        def retrieve(query, **_kwargs):
            return [
                {
                    "message_id": "m1",
                    "subject": "SDGE bill September",
                    "vec_rank": hp.MISSING_RANK,
                    "fts_rank": 1,
                }
            ]

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = hp.run_health(
                db,
                skip_writers=True,
                retrieve_fn=retrieve,
                embed_probe_fn=lambda _q: ([0.2] * 8, None),
                ollama_up=True,
            )
        self.assertEqual(report.exit_code(), 0)
        self.assertEqual(report.hybrid[0].vec_real, 0)
        self.assertTrue(any("vec_rank" in w for w in report.warnings))

    def test_is_real_vec_rank(self):
        self.assertTrue(hp.is_real_vec_rank(1))
        self.assertTrue(hp.is_real_vec_rank(50))
        self.assertFalse(hp.is_real_vec_rank(None))
        self.assertFalse(hp.is_real_vec_rank(1000))
        self.assertFalse(hp.is_real_vec_rank(0))
        self.assertFalse(hp.is_real_vec_rank("nope"))


class WriterScanTests(unittest.TestCase):
    def test_reports_pids_and_does_not_kill(self):
        hits = [
            hp.WriterHit(
                kind="process",
                pid="4242",
                label=None,
                detail="python embed_backfill.py --db mailroom.sqlite",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            report = hp.run_health(
                db,
                skip_hybrid=True,
                writer_scan_fn=lambda: hits,
            )
        self.assertEqual(len(report.writers), 1)
        self.assertEqual(report.writers[0].pid, "4242")
        self.assertIn("embed_backfill", hp.format_report(report))

    def test_parse_ps_and_launchctl(self):
        ps = (
            "  11 /usr/bin/python3 embed_backfill.py --limit 8\n"
            "  12 /usr/bin/python3 sor_health_pack.py\n"
            "  13 /usr/bin/python3 mailroom_daily.py\n"
        )
        procs = hp._parse_ps_lines(ps)
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].pid, "11")
        launch = (
            "PID\tStatus\tLabel\n"
            "88\t0\tcom.mailroom.embed-rem\n"
            "-\t0\tcom.apple.something\n"
        )
        agents = hp._parse_launchctl_lines(launch)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].pid, "88")
        self.assertEqual(agents[0].label, "com.mailroom.embed-rem")

    def test_plist_scan_uses_injected_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.mailroom.embed-shard.plist").write_text("x", encoding="utf-8")
            (agents / "com.user.other.plist").write_text("x", encoding="utf-8")
            found = hp.scan_embed_writers(home=home, run=lambda _cmd: None)
        labels = [hit.label for hit in found]
        self.assertIn("com.mailroom.embed-shard", labels)
        self.assertNotIn("com.user.other", labels)


class CliTests(unittest.TestCase):
    def test_cli_skip_hybrid_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "sor_health_pack.py"),
                    "--db",
                    str(db),
                    "--skip-hybrid",
                    "--skip-writers",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("integrity: ok", proc.stdout)
        self.assertIn("messages=4", proc.stdout)
        self.assertNotIn(SECRET_BODY, proc.stdout)
        self.assertNotIn("/Users/buck", proc.stdout)

    def test_cli_json_and_hybrid_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _fixture(db)
            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = hp.main(
                    [
                        "--db",
                        str(db),
                        "--json",
                        "--skip-writers",
                    ]
                )
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["integrity_ok"])
        self.assertEqual(payload["messages"], 4)
        self.assertNotIn(SECRET_BODY, buf.getvalue())
        self.assertTrue(payload["hybrid"])
        self.assertTrue(any(item.get("fail_open") for item in payload["hybrid"]))

    def test_help_mentions_env_and_home(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "sor_health_pack.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MAILROOM_DB", proc.stdout)
        self.assertIn("$HOME/MailArchive/mailroom.sqlite", proc.stdout)
        self.assertNotIn("/Users/buck", proc.stdout)


class HygieneTests(unittest.TestCase):
    def test_health_sources_have_no_secrets_or_home_hardcodes(self):
        files = (
            SCRIPTS / "sor_health_pack.py",
            ROOT / "docs" / "sor-health.md",
            ROOT / "README.md",
        )
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN:
                self.assertNotIn(token, text, msg="%s: %s" % (path.name, token))

    def test_script_uses_home_and_mailroom_db(self):
        text = (SCRIPTS / "sor_health_pack.py").read_text(encoding="utf-8")
        self.assertIn("MAILROOM_DB", text)
        self.assertIn("Path.home()", text)
        self.assertIn("expanduser", text)
        self.assertIn("el.connect_db", text)
        self.assertIn("ss.retrieve", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("kill -", text)
        self.assertNotIn("launchctl bootout", text)
        self.assertNotIn("launchctl kill", text)

    def test_docs_label_mbp_and_mini(self):
        docs = (ROOT / "docs" / "sor-health.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("# MBP — SoR health + hybrid smoke", docs)
        self.assertIn("# Mini —", docs)
        self.assertIn("$HOME/MailArchive/mailroom.sqlite", docs)
        self.assertIn("~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py", docs)
        self.assertIn("copy", docs.lower())
        self.assertIn("sor-health.md", readme)
        self.assertIn("# MBP — SoR health + hybrid smoke", readme)

    def test_existing_denylist_still_covers_kirkbacon(self):
        daily = (ROOT / "tests" / "test_mailroom_daily.py").read_text(encoding="utf-8")
        incremental = (ROOT / "tests" / "test_embed_incremental.py").read_text(
            encoding="utf-8"
        )
        plist = (ROOT / "tests" / "test_daily_plist.py").read_text(encoding="utf-8")
        self.assertIn("kirkbacon", daily)
        self.assertIn("kirkbacon", incremental)
        self.assertIn("kirkbacon", plist)


if __name__ == "__main__":
    unittest.main()
