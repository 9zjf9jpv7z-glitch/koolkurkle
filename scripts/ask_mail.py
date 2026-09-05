#!/usr/bin/env python3
"""ask_mail stub: hybrid retrieve over Mini-local mailroom.sqlite.

FTS first (messages_fts), then optional sqlite-vec cosine hits when
embed_lib / semantic_search is on sys.path. LM Studio generation is not
wired — this only retrieves.

Skip lane=auth / auth-shaped rows when the messages table has a lane
column. No IMAP. No secrets.

  ~/MailArchive/.venv/bin/python ask_mail.py 'receipt from apple'
  ~/MailArchive/.venv/bin/python ask_mail.py --k 8 --fts-only 'invoice'
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / "MailArchive" / "mailroom.sqlite"
DEFAULT_K = 8
AUTH_LANE_RE = re.compile(r"^(auth|2fa|otp|verification)$", re.I)
AUTH_SUBJECT_RE = re.compile(
    r"\b(verification code|one[- ]time (code|password)|your code is|"
    r"2fa|two[- ]factor|sign[- ]in code|security code)\b",
    re.I,
)


class AskMailError(RuntimeError):
    """Retrieve failure (never includes secrets)."""


def default_db_path() -> Path:
    return Path.home() / "MailArchive" / "mailroom.sqlite"


def connect(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise AskMailError("database not found: %s" % db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}


def is_auth_shaped(lane: str | None, subject: str | None) -> bool:
    if lane and AUTH_LANE_RE.match(lane.strip()):
        return True
    if subject and AUTH_SUBJECT_RE.search(subject):
        return True
    return False


def fts_hits(conn: sqlite3.Connection, query: str, k: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "messages_fts"):
        return []
    fts_cols = columns(conn, "messages_fts")
    has_messages = table_exists(conn, "messages")
    msg_cols = columns(conn, "messages") if has_messages else set()
    select_subject = "fts.subject" if "subject" in fts_cols else "NULL"
    if "body" in fts_cols:
        snippet_expr = "substr(COALESCE(fts.body, ''), 1, 180)"
    elif "subject" in fts_cols:
        snippet_expr = "substr(COALESCE(fts.subject, ''), 1, 180)"
    else:
        snippet_expr = "NULL"
    join = ""
    lane_sel = "NULL AS lane"
    from_sel = "NULL AS from_addr"
    if has_messages:
        join = "LEFT JOIN messages m ON m.id = fts.id"
        if "lane" in msg_cols:
            lane_sel = "m.lane AS lane"
        if "from_addr" in msg_cols:
            from_sel = "m.from_addr AS from_addr"
        elif "from" in msg_cols:
            from_sel = 'm."from" AS from_addr'
        if "subject" in msg_cols and "subject" not in fts_cols:
            select_subject = "m.subject"
    sql = (
        "SELECT fts.id AS id, %s AS subject, %s, %s, %s AS snippet "
        "FROM messages_fts fts %s "
        "WHERE messages_fts MATCH ? LIMIT ?"
    ) % (select_subject, lane_sel, from_sel, snippet_expr, join)
    try:
        rows = conn.execute(sql, (query, k * 3)).fetchall()
    except sqlite3.Error:
        # MATCH syntax can fail on odd queries; fall back to LIKE.
        like = "%" + query.replace("%", "") + "%"
        like_sql = (
            "SELECT fts.id AS id, %s AS subject, %s, %s, "
            "substr(COALESCE(fts.body, ''), 1, 180) AS snippet "
            "FROM messages_fts fts %s "
            "WHERE COALESCE(fts.body, '') LIKE ? OR COALESCE(fts.subject, '') LIKE ? "
            "LIMIT ?"
        ) % (select_subject, lane_sel, from_sel, join)
        try:
            rows = conn.execute(like_sql, (like, like, k * 3)).fetchall()
        except sqlite3.Error:
            return []
    hits: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        if is_auth_shaped(rec.get("lane"), rec.get("subject")):
            continue
        hits.append(
            {
                "id": rec.get("id"),
                "subject": rec.get("subject") or "",
                "from_addr": rec.get("from_addr") or "",
                "snippet": rec.get("snippet") or "",
                "source": "fts",
                "score": None,
            }
        )
        if len(hits) >= k:
            break
    return hits


def semantic_hits(
    db: Path, query: str, k: int, extension_path: str | None
) -> list[dict[str, Any]]:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from embed_lib import semantic_search as _semantic_search
    except ImportError:
        return []
    try:
        raw = _semantic_search(
            query,
            db,
            k=k * 2,
            extension_path=extension_path,
        )
    except Exception:
        return []
    hits: list[dict[str, Any]] = []
    for item in raw or []:
        subject = item.get("subject") or ""
        lane = item.get("lane")
        if is_auth_shaped(lane, subject):
            continue
        hits.append(
            {
                "id": item.get("id"),
                "subject": subject,
                "from_addr": item.get("from_addr") or "",
                "snippet": item.get("snippet") or "",
                "source": "vec",
                "score": item.get("score"),
            }
        )
        if len(hits) >= k:
            break
    return hits


def merge_hits(fts: list[dict[str, Any]], vec: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Interleave FTS and vec, unique by id, FTS first on ties."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    queues = [list(fts), list(vec)]
    while len(out) < k and any(queues):
        for q in queues:
            if len(out) >= k or not q:
                continue
            item = q.pop(0)
            key = str(item.get("id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "no hits (FTS empty / embeddings not backfilled; LM Studio not wired)\n"
    lines = []
    for i, hit in enumerate(hits, start=1):
        score = hit.get("score")
        score_s = "" if score is None else "  score=%.3f" % float(score)
        lines.append(
            "%d. [%s]%s  %s" % (i, hit.get("source"), score_s, hit.get("subject") or "(no subject)")
        )
        lines.append("   id=%s  from=%s" % (hit.get("id"), hit.get("from_addr") or "-"))
        snippet = (hit.get("snippet") or "").replace("\n", " ").strip()
        if snippet:
            lines.append("   %s" % snippet[:180])
    lines.append("# LM Studio generation is not wired; this is retrieve-only.")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid FTS + optional sqlite-vec retrieve. "
            "LM Studio later. Skip auth-shaped rows."
        )
    )
    parser.add_argument("query", help="Search string (FTS MATCH / embed query).")
    parser.add_argument(
        "--db",
        default=str(default_db_path()),
        help="Mailroom SQLite path (default: ~/MailArchive/mailroom.sqlite).",
    )
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="Max hits (default: %s)." % DEFAULT_K)
    parser.add_argument(
        "--fts-only",
        action="store_true",
        help="Skip sqlite-vec even if embed_lib is present.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON lines.",
    )
    parser.add_argument(
        "--vec-extension",
        default=None,
        help="Optional vec0 dylib/so for embed_lib.",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Reserved. LM Studio is not wired; exits 2 if passed without --allow-llm-stub.",
    )
    parser.add_argument(
        "--allow-llm-stub",
        action="store_true",
        help="With --llm, print the retrieve stub note and still retrieve only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.llm and not args.allow_llm_stub:
        sys.stderr.write(
            "error: LM Studio is not wired yet. Retrieve only "
            "(omit --llm, or pass --allow-llm-stub).\n"
        )
        return 2
    db = Path(args.db).expanduser()
    try:
        conn = connect(db)
    except AskMailError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        fts = fts_hits(conn, args.query, args.k)
    finally:
        conn.close()
    vec: list[dict[str, Any]] = []
    if not args.fts_only:
        vec = semantic_hits(db, args.query, args.k, args.vec_extension)
    hits = merge_hits(fts, vec, args.k)
    if args.json:
        for hit in hits:
            sys.stdout.write(json.dumps(hit, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(format_hits(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
