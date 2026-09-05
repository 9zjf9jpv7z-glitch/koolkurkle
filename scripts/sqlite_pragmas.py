#!/usr/bin/env python3
"""MAILROOM.md §9 SQLite PRAGMAs for mailroom.sqlite.

Writers: Homebrew / venv Python sqlite ≥ 3.51.3 (inspect: 3.53.4).
Apple /usr/bin/sqlite3 and Apple python sqlite 3.51.0 are below the pin —
do not use them as writers.

  apply_writer_pragmas(conn)  — WAL, synchronous=FULL, …
  apply_reader_pragmas(conn)  — WAL, synchronous=NORMAL, …

Shared: busy_timeout=30000; foreign_keys=ON; mmap_size=0; temp_store=MEMORY.

ask_mail may call apply_reader_pragmas; it does not take the writer lock.
This module is not wired into embed_backfill in PR-0.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

# MAILROOM.md §9 writer pin. Homebrew/venv inspect: 3.53.4.
WRITER_SQLITE_MIN = (3, 51, 3)
BUSY_TIMEOUT_MS = 30000
MMAP_SIZE = 0


def sqlite_version_tuple(conn: sqlite3.Connection | None = None) -> tuple[int, ...]:
    raw = sqlite3.sqlite_version if conn is None else conn.execute("SELECT sqlite_version()").fetchone()[0]
    return tuple(int(p) for p in str(raw).split("."))


def writer_sqlite_ok(conn: sqlite3.Connection | None = None) -> bool:
    return sqlite_version_tuple(conn) >= WRITER_SQLITE_MIN


def require_writer_sqlite(conn: sqlite3.Connection | None = None) -> None:
    ver = sqlite_version_tuple(conn)
    if ver < WRITER_SQLITE_MIN:
        raise RuntimeError(
            "sqlite %s is below writer pin %s (use Homebrew/venv python; "
            "Apple CLI/py 3.51.0 is below pin)"
            % (".".join(map(str, ver)), ".".join(map(str, WRITER_SQLITE_MIN)))
        )


def _exec_pragma(conn: sqlite3.Connection, sql: str) -> Any:
    cur = conn.execute(sql)
    try:
        return cur.fetchone()
    finally:
        cur.close()


def apply_common_pragmas(conn: sqlite3.Connection) -> None:
    _exec_pragma(conn, "PRAGMA journal_mode=WAL")
    _exec_pragma(conn, "PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    _exec_pragma(conn, "PRAGMA foreign_keys=ON")
    _exec_pragma(conn, "PRAGMA mmap_size=%d" % MMAP_SIZE)
    _exec_pragma(conn, "PRAGMA temp_store=MEMORY")


def apply_writer_pragmas(conn: sqlite3.Connection) -> None:
    apply_common_pragmas(conn)
    _exec_pragma(conn, "PRAGMA synchronous=FULL")


def apply_reader_pragmas(conn: sqlite3.Connection) -> None:
    apply_common_pragmas(conn)
    _exec_pragma(conn, "PRAGMA synchronous=NORMAL")


def read_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    names = (
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "foreign_keys",
        "mmap_size",
        "temp_store",
        "user_version",
        "page_size",
    )
    out: dict[str, Any] = {}
    for name in names:
        row = _exec_pragma(conn, "PRAGMA %s" % name)
        out[name] = None if row is None else row[0]
    out["sqlite_version"] = conn.execute("SELECT sqlite_version()").fetchone()[0]
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply or show MAILROOM §9 SQLite PRAGMAs (no secrets)."
    )
    parser.add_argument(
        "--db",
        help="SQLite path (required for --apply / --show).",
    )
    parser.add_argument(
        "--apply",
        choices=("writer", "reader"),
        help="Apply writer or reader PRAGMAs to --db.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print current PRAGMAs for --db.",
    )
    parser.add_argument(
        "--check-writer-version",
        action="store_true",
        help="Exit 0 if this interpreter's sqlite ≥ 3.51.3, else 2.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_writer_version:
        ver = sqlite_version_tuple()
        sys.stdout.write(
            "sqlite %s pin %s %s\n"
            % (
                ".".join(map(str, ver)),
                ".".join(map(str, WRITER_SQLITE_MIN)),
                "ok" if writer_sqlite_ok() else "below",
            )
        )
        return 0 if writer_sqlite_ok() else 2
    if args.apply or args.show:
        if not args.db:
            sys.stderr.write("error: --db is required with --apply / --show\n")
            return 2
        conn = sqlite3.connect(args.db)
        try:
            if args.apply == "writer":
                apply_writer_pragmas(conn)
            elif args.apply == "reader":
                apply_reader_pragmas(conn)
            if args.show or args.apply:
                pragmas = read_pragmas(conn)
                for key in sorted(pragmas):
                    sys.stdout.write("%s=%s\n" % (key, pragmas[key]))
        finally:
            conn.close()
        return 0
    build_parser().print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
