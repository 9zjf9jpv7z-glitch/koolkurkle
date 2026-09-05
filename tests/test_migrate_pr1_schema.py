#!/usr/bin/env python3
"""PR-1 additive schema tests. Temp DB only — no SoR, no vec0 rebuild."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PR0_SCHEMA = ROOT / "docs" / "pr0" / "mailroom_schema.sql"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import migrate_pr1_schema as mig  # noqa: E402


LIVE_MESSAGES_COLS = {
    "id",
    "source",
    "folder",
    "date_utc",
    "from_addr",
    "from_name",
    "to_addrs",
    "cc_addrs",
    "subject",
    "snippet",
    "size_bytes",
    "lane",
    "urgent",
    "junk",
    "injection_flag",
    "body_path",
    "jsonl_offset",
    "jsonl_len",
    "uid",
    "flags",
    "present_on_server",
    "message_id_header",
    "in_reply_to",
    "content_hash",
    "ingested_at",
}

LIVE_EMBED_COLS = {
    "message_id",
    "model",
    "model_version",
    "created_at",
    "text_hash",
    "char_count",
    "dims",
}

PR1_MESSAGES_COLS = {
    "thread_id",
    "references_header",
    "cleaned_body",
    "cleaned_chars",
    "has_attachments",
}

PR1_EMBED_COLS = {
    "embed_model",
    "embed_dim",
    "embed_quant",
    "instruct_version",
    "quote_stripped",
    "content_hash",
    "chunk_id",
    "source",
}


def apply_pr0_live_schema_minus_vec0(conn: sqlite3.Connection) -> None:
    """Apply docs/pr0/mailroom_schema.sql except vec0 (CI has no sqlite-vec)."""
    raw = PR0_SCHEMA.read_text(encoding="utf-8")
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            continue
        lower = stripped.lower()
        if "vec0" in lower or "sqlite-vec" in lower:
            continue
        kept.append(line)
    conn.executescript("\n".join(kept))
    conn.execute("PRAGMA user_version = 0")


def live_like_db(tmp: str) -> Path:
    db = Path(tmp) / "mailroom.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        apply_pr0_live_schema_minus_vec0(conn)
        # Sentinel so we can prove vec0 was not dropped/recreated.
        conn.execute(
            "CREATE TABLE message_embeddings (message_id TEXT PRIMARY KEY, note TEXT)"
        )
        conn.execute(
            "INSERT INTO message_embeddings(message_id, note) VALUES ('keep-me', 'live')"
        )
        conn.commit()
    finally:
        conn.close()
    return db


def cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}


def master_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return None if row is None else row[0]


def index_sqls(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    return [str(r[0]) for r in rows]


class LiveSchemaFixtureTests(unittest.TestCase):
    def test_pr0_dump_is_on_main_tree(self):
        self.assertTrue(PR0_SCHEMA.is_file(), "docs/pr0/mailroom_schema.sql missing")
        text = PR0_SCHEMA.read_text(encoding="utf-8")
        self.assertIn("date_utc", text)
        self.assertIn("in_reply_to", text)
        self.assertIn("content_hash", text)
        self.assertIn("idx_messages_date ON messages(date_utc)", text)
        self.assertIn("USING vec0", text)
        self.assertIn("USING fts5", text)
        self.assertIn("porter unicode61", text)


class MigrateAdditiveTests(unittest.TestCase):
    def test_adds_columns_tables_and_bumps_user_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            conn = sqlite3.connect(str(db))
            try:
                fts_before = master_sql(conn, "messages_fts")
                vec_before = master_sql(conn, "message_embeddings")
                self.assertEqual(mig.user_version(conn), 0)
                report = mig.migrate(conn)
                conn.commit()
                self.assertEqual(report["user_version_before"], 0)
                self.assertEqual(report["user_version_after"], 1)
                self.assertEqual(mig.user_version(conn), 1)
                self.assertEqual(report["messages_fts"], "unchanged")
                self.assertEqual(report["message_embeddings"], "untouched")

                msg_cols = cols(conn, "messages")
                self.assertTrue(LIVE_MESSAGES_COLS <= msg_cols)
                self.assertTrue(PR1_MESSAGES_COLS <= msg_cols)
                self.assertIn("date_utc", msg_cols)
                self.assertNotIn("date", msg_cols)

                embed_cols = cols(conn, "embedding_meta")
                self.assertTrue(LIVE_EMBED_COLS <= embed_cols)
                self.assertTrue(PR1_EMBED_COLS <= embed_cols)
                # Do not rename live model/dims.
                self.assertIn("model", embed_cols)
                self.assertIn("model_version", embed_cols)
                self.assertIn("dims", embed_cols)

                self.assertEqual(report["actions"]["messages.in_reply_to"], "skipped (exists)")
                self.assertEqual(report["actions"]["messages.content_hash"], "skipped (exists)")
                self.assertEqual(report["actions"]["messages.thread_id"], "added")
                self.assertEqual(report["actions"]["embedding_meta.embed_model"], "added")
                self.assertEqual(report["actions"]["chunk_vec_map"], "created")

                map_cols = cols(conn, "chunk_vec_map")
                self.assertEqual(map_cols, {"chunk_id", "vec_rowid", "message_id"})
                pk = [
                    r[1]
                    for r in conn.execute("PRAGMA table_info(chunk_vec_map)")
                    if r[5]
                ]
                self.assertEqual(pk, ["chunk_id"])

                for table in ("chunks", "attachments", "pipeline_runs", "ask_audit", "messages_ids"):
                    self.assertTrue(mig.table_exists(conn, table), table)

                ids_sql = master_sql(conn, "messages_ids")
                self.assertIsNotNone(ids_sql)
                self.assertIn("fts5", ids_sql.lower())
                self.assertIn("unicode61", ids_sql.lower())
                self.assertNotIn("porter", ids_sql.lower())

                self.assertEqual(master_sql(conn, "messages_fts"), fts_before)
                self.assertIn("porter unicode61", fts_before or "")
                self.assertEqual(master_sql(conn, "message_embeddings"), vec_before)
                note = conn.execute(
                    "SELECT note FROM message_embeddings WHERE message_id='keep-me'"
                ).fetchone()
                self.assertEqual(note[0], "live")

                sqls = "\n".join(index_sqls(conn)).lower()
                self.assertIn("idx_messages_thread", sqls)
                self.assertIn("messages(thread_id)", sqls)
                self.assertIn("idx_chunk_vec_map_message_id", sqls)
                # Broken live-name miss: messages(date) must not appear.
                self.assertNotIn("messages(date)", sqls.replace("messages(date_utc)", ""))
                self.assertIn("idx_messages_date on messages(date_utc)", sqls)
            finally:
                conn.close()

    def test_idempotent_second_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            conn = sqlite3.connect(str(db))
            try:
                first = mig.migrate(conn)
                conn.commit()
                second = mig.migrate(conn)
                conn.commit()
                self.assertEqual(second["user_version_after"], 1)
                self.assertEqual(second["actions"]["user_version"], "unchanged (1)")
                self.assertEqual(second["actions"]["messages.thread_id"], "skipped (exists)")
                self.assertEqual(second["actions"]["messages.in_reply_to"], "skipped (exists)")
                self.assertEqual(second["actions"]["embedding_meta.source"], "skipped (exists)")
                self.assertEqual(second["actions"]["chunk_vec_map"], "exists")
                self.assertEqual(second["actions"]["messages_ids"], "exists")
                self.assertEqual(first["message_embeddings"], "untouched")
                self.assertEqual(second["message_embeddings"], "untouched")
            finally:
                conn.close()

    def test_duplicate_column_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            conn = sqlite3.connect(str(db))
            try:
                status = mig.add_column(conn, "messages", "in_reply_to", "TEXT")
                self.assertEqual(status, "skipped (exists)")
                # Force the OperationalError path: PRAGMA lies if we skip the
                # pre-check by using a raw ALTER after the column exists.
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    conn.execute("ALTER TABLE messages ADD COLUMN in_reply_to TEXT")
                self.assertTrue(mig._duplicate_column(ctx.exception))
            finally:
                conn.close()

    def test_has_attachments_and_source_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            conn = sqlite3.connect(str(db))
            try:
                mig.migrate(conn)
                conn.execute(
                    "INSERT INTO messages(id, source) VALUES ('m1', 'icloud')"
                )
                row = conn.execute(
                    "SELECT has_attachments, thread_id FROM messages WHERE id='m1'"
                ).fetchone()
                self.assertEqual(row[0], 0)
                self.assertIsNone(row[1])
                conn.execute(
                    "INSERT INTO embedding_meta(message_id, model, model_version, "
                    "created_at, text_hash, char_count, dims) "
                    "VALUES ('m1', 'qwen', 'v1', 't', 'h', 0, 1024)"
                )
                src = conn.execute(
                    "SELECT source, quote_stripped, model, dims "
                    "FROM embedding_meta WHERE message_id='m1'"
                ).fetchone()
                self.assertEqual(src[0], "message")
                self.assertEqual(src[1], 0)
                self.assertEqual(src[2], "qwen")
                self.assertEqual(src[3], 1024)
            finally:
                conn.close()

    def test_chunk_vec_map_unique_vec_rowid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            conn = sqlite3.connect(str(db))
            try:
                mig.migrate(conn)
                conn.execute(
                    "INSERT INTO chunk_vec_map(chunk_id, vec_rowid, message_id) "
                    "VALUES ('c1', 1, 'm1')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO chunk_vec_map(chunk_id, vec_rowid, message_id) "
                        "VALUES ('c2', 1, 'm1')"
                    )
            finally:
                conn.close()

    def test_missing_core_tables_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty.sqlite"
            conn = sqlite3.connect(str(db))
            try:
                with self.assertRaises(mig.MigrateError):
                    mig.migrate(conn)
            finally:
                conn.close()


class SourceHardFailTests(unittest.TestCase):
    def test_migration_source_never_rebuilds_vec0_or_renames(self):
        src = (SCRIPTS / "migrate_pr1_schema.py").read_text(encoding="utf-8")
        lowered = src.lower()
        self.assertNotIn("drop table message_embeddings", lowered)
        self.assertNotIn("drop table if exists message_embeddings", lowered)
        self.assertNotIn("create virtual table message_embeddings", lowered)
        self.assertNotIn("create virtual table if not exists message_embeddings", lowered)
        self.assertNotIn("rename column", lowered)
        self.assertNotIn("model→embed_model", src)
        self.assertNotIn('rename table', lowered)
        # No secrets.
        self.assertNotIn("sk-", src)
        self.assertNotIn("BEGIN OPENSSH", src)
        self.assertNotIn("app-password", src)
        # Must include the vec0 bridge.
        self.assertIn("chunk_vec_map", src)
        self.assertIn("user_version", src)


class CliTests(unittest.TestCase):
    def test_cli_positional_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "migrate_pr1_schema.py"), str(db)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("user_version: 0 -> 1", proc.stdout)
            self.assertIn("messages.in_reply_to: skipped (exists)", proc.stdout)
            self.assertIn("messages.content_hash: skipped (exists)", proc.stdout)
            self.assertIn("chunk_vec_map: created", proc.stdout)
            self.assertIn("message_embeddings=untouched", proc.stdout)
            again = subprocess.run(
                [sys.executable, str(SCRIPTS / "migrate_pr1_schema.py"), "--db", str(db)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("user_version: 1 -> 1", again.stdout)
            self.assertIn("chunk_vec_map: exists", again.stdout)

    def test_cli_lock_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "migrate_pr1_schema.py"),
                    "--db",
                    str(db),
                    "--lock",
                    "--lock-file",
                    str(lock),
                    "--action-required-file",
                    str(action),
                    "--purpose",
                    "pr1-unit",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("chunk_vec_map: created", proc.stdout)
            self.assertTrue(lock.is_file())

    def test_ci_refuses_default_sor_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = live_like_db(tmp)
            old_default = mig.DEFAULT_DB
            old_ci = os.environ.get("CI")
            try:
                mig.DEFAULT_DB = db
                os.environ["CI"] = "true"
                with self.assertRaises(mig.MigrateError) as ctx:
                    mig.migrate_path(db)
                self.assertIn("refuse", str(ctx.exception))
                self.assertIn("CI", str(ctx.exception))
            finally:
                mig.DEFAULT_DB = old_default
                if old_ci is None:
                    os.environ.pop("CI", None)
                else:
                    os.environ["CI"] = old_ci


if __name__ == "__main__":
    raise SystemExit(unittest.main())
