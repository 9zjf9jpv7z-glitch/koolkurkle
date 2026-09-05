#!/usr/bin/env python3
"""PR-1 additive DDL (MAILROOM.md §10, Heavy-patched 2026-09-05).

Idempotent schema-only migrate for mailroom.sqlite. Script only — do not
apply against production from CI.

Live SoR names (docs/pr0/mailroom_schema.sql, PR-0 #13):
  messages.date_utc (not date); in_reply_to + content_hash already exist;
  idx_messages_date already on date_utc; embedding_meta.model / model_version
  / dims stay — ADD embed_* / quote_stripped / chunk_id / source, do not
  rename. messages_fts unchanged. Live vec0 (message_embeddings) is
  message_id-keyed — do not drop / rebuild / recreate.

  python3 scripts/migrate_pr1_schema.py
  python3 scripts/migrate_pr1_schema.py /path/to/mailroom.sqlite
  python3 scripts/migrate_pr1_schema.py --db /path/to/mailroom.sqlite

Optional: wrap with with_writer_lock.py --purpose pr1_schema -- ...
or pass --lock (uses PR-0 helper if present).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DEFAULT_DB = Path.home() / "MailArchive" / "mailroom.sqlite"
TARGET_USER_VERSION = 1

# Live columns already on MBP SoR — never ALTER these (duplicate-column skip
# is also safe if a clone is missing them).
MESSAGES_SKIP_EXISTING = ("in_reply_to", "content_hash")

MESSAGES_ADD: tuple[tuple[str, str], ...] = (
    ("thread_id", "TEXT"),
    ("in_reply_to", "TEXT"),
    ("references_header", "TEXT"),
    ("cleaned_body", "TEXT"),
    ("cleaned_chars", "INTEGER"),
    ("has_attachments", "INTEGER DEFAULT 0"),
    ("content_hash", "TEXT"),
)

# New indexes only. Do not create messages(date) — live idx_messages_date
# is on date_utc. lane/date already exist (skip / IF NOT EXISTS on live names).
MESSAGES_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_messages_thread", "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)"),
    ("idx_messages_chash", "CREATE INDEX IF NOT EXISTS idx_messages_chash ON messages(content_hash)"),
)

EMBEDDING_META_ADD: tuple[tuple[str, str], ...] = (
    ("embed_model", "TEXT"),
    ("embed_dim", "INTEGER"),
    ("embed_quant", "TEXT"),
    ("instruct_version", "TEXT"),
    ("quote_stripped", "INTEGER DEFAULT 0"),
    ("content_hash", "TEXT"),
    ("chunk_id", "TEXT"),
    ("source", "TEXT DEFAULT 'message'"),
)

NEW_TABLE_SQL: tuple[tuple[str, str], ...] = (
    (
        "chunks",
        """
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          chunk_index INTEGER NOT NULL DEFAULT 0,
          text TEXT,
          char_count INTEGER NOT NULL DEFAULT 0,
          content_hash TEXT,
          quote_stripped INTEGER NOT NULL DEFAULT 0,
          source TEXT NOT NULL DEFAULT 'message',
          created_at TEXT
        )
        """,
    ),
    (
        "attachments",
        """
        CREATE TABLE IF NOT EXISTS attachments (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          filename TEXT,
          mime_type TEXT,
          size_bytes INTEGER,
          content_hash TEXT,
          path TEXT,
          created_at TEXT
        )
        """,
    ),
    (
        "pipeline_runs",
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
          id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          actor TEXT,
          host TEXT,
          tool TEXT,
          step TEXT,
          status TEXT,
          detail TEXT
        )
        """,
    ),
    (
        "ask_audit",
        """
        CREATE TABLE IF NOT EXISTS ask_audit (
          ts TEXT NOT NULL,
          actor TEXT,
          query TEXT,
          k INTEGER,
          hit_count INTEGER,
          hit_ids TEXT,
          detail TEXT
        )
        """,
    ),
    (
        "chunk_vec_map",
        """
        CREATE TABLE IF NOT EXISTS chunk_vec_map (
          chunk_id TEXT PRIMARY KEY,
          vec_rowid INTEGER UNIQUE,
          message_id TEXT NOT NULL
        )
        """,
    ),
)

NEW_INDEX_SQL: tuple[tuple[str, str], ...] = (
    ("idx_chunks_message_id", "CREATE INDEX IF NOT EXISTS idx_chunks_message_id ON chunks(message_id)"),
    (
        "idx_attachments_message_id",
        "CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id)",
    ),
    (
        "idx_pipeline_runs_started",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at)",
    ),
    (
        "idx_chunk_vec_map_message_id",
        "CREATE INDEX IF NOT EXISTS idx_chunk_vec_map_message_id ON chunk_vec_map(message_id)",
    ),
)

# unicode61, no porter — Message-ID tokens must not be stemmed.
MESSAGES_IDS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_ids USING fts5("
    "id UNINDEXED, message_id, tokenize='unicode61')"
)


class MigrateError(RuntimeError):
    """Schema migrate failure (never includes secrets)."""


def default_db_path() -> Path:
    raw = os.environ.get("MAILROOM_DB")
    return Path(raw).expanduser() if raw else DEFAULT_DB


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA user_version = N cannot be bound as a parameter.
    conn.execute("PRAGMA user_version = %d" % int(version))


def _duplicate_column(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "duplicate column" in msg


def add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> str:
    """ALTER TABLE ADD COLUMN. Skip if the column already exists."""
    existing = columns(conn, table)
    if name in existing:
        return "skipped (exists)"
    try:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    except sqlite3.OperationalError as exc:
        if _duplicate_column(exc):
            return "skipped (duplicate column)"
        raise
    return "added"


def _maybe_apply_writer_pragmas(conn: sqlite3.Connection) -> str:
    try:
        import sqlite_pragmas
    except ImportError:
        return "skipped (sqlite_pragmas missing)"
    apply = getattr(sqlite_pragmas, "apply_writer_pragmas", None)
    if apply is None:
        return "skipped (no apply_writer_pragmas)"
    apply(conn)
    return "applied"


def _ci_refuses_default_sor(db: Path) -> None:
    if not (os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")):
        return
    try:
        resolved = db.expanduser().resolve()
        default = DEFAULT_DB.expanduser().resolve()
    except OSError:
        resolved = db.expanduser()
        default = DEFAULT_DB.expanduser()
    if resolved == default:
        raise MigrateError("refuse: will not migrate default SoR path under CI")


def migrate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Apply §10 additive DDL. Never touches message_embeddings / messages_fts."""
    report: dict[str, Any] = {"actions": {}, "pragmas": _maybe_apply_writer_pragmas(conn)}
    if not table_exists(conn, "messages"):
        raise MigrateError("messages table missing — not a mailroom SoR")
    if not table_exists(conn, "embedding_meta"):
        raise MigrateError("embedding_meta table missing — not a mailroom SoR")

    before_uv = user_version(conn)
    report["user_version_before"] = before_uv

    fts_sql_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone()
    vec_sql_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_embeddings'"
    ).fetchone()

    for name, decl in MESSAGES_ADD:
        key = "messages.%s" % name
        report["actions"][key] = add_column(conn, "messages", name, decl)

    for idx_name, sql in MESSAGES_INDEXES:
        conn.execute(sql)
        report["actions"][idx_name] = "ensured"

    for name, decl in EMBEDDING_META_ADD:
        key = "embedding_meta.%s" % name
        report["actions"][key] = add_column(conn, "embedding_meta", name, decl)

    for table, sql in NEW_TABLE_SQL:
        existed = table_exists(conn, table)
        conn.execute(sql)
        report["actions"][table] = "exists" if existed else "created"

    ids_existed = table_exists(conn, "messages_ids")
    conn.execute(MESSAGES_IDS_SQL)
    report["actions"]["messages_ids"] = "exists" if ids_existed else "created"

    for idx_name, sql in NEW_INDEX_SQL:
        conn.execute(sql)
        report["actions"][idx_name] = "ensured"

    if before_uv < TARGET_USER_VERSION:
        set_user_version(conn, TARGET_USER_VERSION)
        report["user_version_after"] = TARGET_USER_VERSION
        report["actions"]["user_version"] = "bumped %d→%d" % (before_uv, TARGET_USER_VERSION)
    else:
        report["user_version_after"] = before_uv
        report["actions"]["user_version"] = "unchanged (%d)" % before_uv

    fts_sql_after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
    ).fetchone()
    vec_sql_after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='message_embeddings'"
    ).fetchone()
    if fts_sql_before != fts_sql_after:
        raise MigrateError("messages_fts sql changed — refuse")
    if vec_sql_before != vec_sql_after:
        raise MigrateError("message_embeddings sql changed — refuse (no vec0 rebuild)")
    report["messages_fts"] = "unchanged"
    report["message_embeddings"] = "untouched"
    return report


def format_report(report: dict[str, Any], db: Path) -> str:
    lines = [
        "pr1 schema migrate",
        "db=%s" % db,
        "pragmas=%s" % report.get("pragmas"),
        "user_version: %s -> %s"
        % (report.get("user_version_before"), report.get("user_version_after")),
        "messages_fts=%s" % report.get("messages_fts"),
        "message_embeddings=%s" % report.get("message_embeddings"),
    ]
    for key, value in report.get("actions", {}).items():
        lines.append("%s: %s" % (key, value))
    return "\n".join(lines) + "\n"


def _run_locked(purpose: str, lock_file: Path | None, action_required: Path | None, fn: Callable[[], int]) -> int:
    try:
        import with_writer_lock as wwl
    except ImportError as exc:
        raise MigrateError("with_writer_lock missing; omit --lock or wrap via CLI") from exc
    if wwl.action_required_open(action_required or wwl.default_action_required_path()):
        raise MigrateError(
            "action-required open ⇒ no lock / no writes (%s)"
            % (action_required or wwl.default_action_required_path())
        )
    held = wwl.acquire_writer_lock(lock_file or wwl.default_lock_path(), purpose)
    try:
        return fn()
    finally:
        wwl.release_writer_lock(held)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PR-1 additive MAILROOM §10 DDL (idempotent). "
            "Does not rebuild vec0. Does not apply SoR from CI."
        )
    )
    parser.add_argument(
        "db_positional",
        nargs="?",
        default=None,
        help="SoR path (default: ~/MailArchive/mailroom.sqlite).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SoR path (overrides positional; default: ~/MailArchive/mailroom.sqlite).",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help="Take PR-0 writer lock (optional; or wrap with with_writer_lock.py).",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help="Lock path when --lock is set.",
    )
    parser.add_argument(
        "--action-required-file",
        default=None,
        help="If this file exists and --lock is set, refuse.",
    )
    parser.add_argument(
        "--purpose",
        default="pr1_schema",
        help="Writer-lock purpose (default: pr1_schema).",
    )
    return parser


def migrate_path(db: Path) -> dict[str, Any]:
    _ci_refuses_default_sor(db)
    if not db.is_file():
        raise MigrateError("database not found: %s" % db)
    conn = sqlite3.connect(str(db))
    try:
        report = migrate(conn)
        conn.commit()
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = args.db if args.db is not None else args.db_positional
    db = Path(raw).expanduser() if raw else default_db_path()

    def _go() -> int:
        report = migrate_path(db)
        sys.stdout.write(format_report(report, db))
        return 0

    try:
        if args.lock:
            lock_file = Path(args.lock_file).expanduser() if args.lock_file else None
            action = (
                Path(args.action_required_file).expanduser()
                if args.action_required_file
                else None
            )
            return _run_locked(args.purpose, lock_file, action, _go)
        return _go()
    except MigrateError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
