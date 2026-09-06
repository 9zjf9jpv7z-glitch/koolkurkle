#!/usr/bin/env python3
"""Additive messages_ids helpers (MAILROOM §6.2 / Heavy PR-6).

Live PR-1 table (docs/pr0 + migrate_pr1_schema.py):

  CREATE VIRTUAL TABLE messages_ids USING fts5(
    id UNINDEXED, message_id, tokenize='unicode61')

This module may ADD an ``identifiers`` column (APN / invoice / UUID tokens).
No column renames. FTS5 cannot ALTER ADD COLUMN, so a missing column is
applied by rebuild-copy (same table name, extra column).

  python3 scripts/messages_ids.py --db ~/MailArchive/mailroom.sqlite --backfill

Read-only retrieve() never writes. Backfill is a separate Mac-side step.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_DB = Path.home() / "MailArchive" / "mailroom.sqlite"

# Identifier-shaped queries / extractors (APN, invoice, UUID).
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# CA-style parcel numbers and close variants: 123-456-78, 1234-567-890.
APN_RE = re.compile(r"\b\d{3,4}-\d{3,4}-\d{2,4}\b")
INVOICE_RE = re.compile(
    r"\b(?:inv(?:oice)?[\s#:.-]*)(\d{3,}[A-Za-z0-9-]*)\b",
    re.IGNORECASE,
)
INVOICE_TOKEN_RE = re.compile(r"\bINV[-_][A-Za-z0-9][-A-Za-z0-9]{1,}\b", re.I)

MESSAGES_IDS_WITH_IDENTIFIERS_SQL = (
    "CREATE VIRTUAL TABLE messages_ids USING fts5("
    "id UNINDEXED, message_id, identifiers, tokenize='unicode61')"
)


class MessagesIdsError(RuntimeError):
    """Identifier index failure (never includes secrets)."""


def default_db_path() -> Path:
    raw = os.environ.get("MAILROOM_DB")
    return Path(raw).expanduser() if raw else DEFAULT_DB


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)]


def extract_identifiers(*parts: str | None) -> list[str]:
    """Return unique APN / invoice / UUID tokens in stable first-seen order."""
    text = "\n".join(p for p in parts if p)
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        key = token.strip()
        if not key:
            return
        low = key.lower()
        if low in seen:
            return
        seen.add(low)
        found.append(key)

    for match in UUID_RE.findall(text):
        _add(match)
    for match in APN_RE.findall(text):
        _add(match)
    for match in INVOICE_TOKEN_RE.findall(text):
        _add(match)
    for match in INVOICE_RE.finditer(text):
        _add(match.group(0).strip())
        if match.group(1):
            _add(match.group(1))
    return found


def is_identifier_query(query: str) -> bool:
    """True when the query is APN / invoice / UUID shaped."""
    q = (query or "").strip()
    if not q:
        return False
    if UUID_RE.search(q) or APN_RE.search(q) or INVOICE_TOKEN_RE.search(q):
        return True
    if INVOICE_RE.search(q):
        return True
    return False


def identifier_match_terms(query: str) -> list[str]:
    """Quoted FTS5 phrases for messages_ids MATCH."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in extract_identifiers(query):
        phrase = token.replace('"', " ").strip()
        if not phrase or phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        terms.append('"%s"' % phrase)
    return terms


def ensure_messages_ids(conn: sqlite3.Connection) -> str:
    """Create messages_ids or add ``identifiers`` additively. Never renames columns."""
    if not table_exists(conn, "messages_ids"):
        conn.execute(MESSAGES_IDS_WITH_IDENTIFIERS_SQL)
        return "created"
    cols = columns(conn, "messages_ids")
    if "identifiers" in cols:
        return "exists"
    rows = conn.execute(
        "SELECT id, message_id FROM messages_ids"
    ).fetchall()
    conn.execute("DROP TABLE messages_ids")
    conn.execute(MESSAGES_IDS_WITH_IDENTIFIERS_SQL)
    for row in rows:
        conn.execute(
            "INSERT INTO messages_ids(id, message_id, identifiers) VALUES (?, ?, ?)",
            (row[0], row[1] or "", ""),
        )
    return "added_identifiers"


def upsert_ids_row(
    conn: sqlite3.Connection,
    message_pk: str,
    *,
    message_id: str | None = None,
    identifiers: Iterable[str] | str | None = None,
) -> None:
    """Replace one messages_ids row. Keeps message_id; fills identifiers."""
    ensure_messages_ids(conn)
    cols = set(columns(conn, "messages_ids"))
    if isinstance(identifiers, str):
        ident_text = identifiers.strip()
    elif identifiers:
        ident_text = " ".join(str(x) for x in identifiers if x)
    else:
        ident_text = ""
    conn.execute("DELETE FROM messages_ids WHERE id = ?", (message_pk,))
    if "identifiers" in cols:
        conn.execute(
            "INSERT INTO messages_ids(id, message_id, identifiers) VALUES (?, ?, ?)",
            (message_pk, message_id or "", ident_text),
        )
    else:
        conn.execute(
            "INSERT INTO messages_ids(id, message_id) VALUES (?, ?)",
            (message_pk, message_id or ""),
        )


def _row_text(conn: sqlite3.Connection, message_pk: str) -> tuple[str | None, str]:
    header = None
    parts: list[str] = []
    if table_exists(conn, "messages"):
        msg_cols = set(columns(conn, "messages"))
        select = ["id"]
        for name in (
            "message_id_header",
            "subject",
            "snippet",
            "from_addr",
            "cleaned_body",
        ):
            if name in msg_cols:
                select.append(name)
        row = conn.execute(
            "SELECT %s FROM messages WHERE id = ?" % ", ".join(select),
            (message_pk,),
        ).fetchone()
        if row is not None:
            mapping = (
                dict(row)
                if isinstance(row, sqlite3.Row)
                else dict(zip(select, row))
            )
            header = mapping.get("message_id_header")
            for key in ("subject", "snippet", "from_addr", "cleaned_body", "message_id_header"):
                val = mapping.get(key)
                if val:
                    parts.append(str(val))
    if table_exists(conn, "messages_fts"):
        fts_cols = set(columns(conn, "messages_fts"))
        fts_sel = ["id"]
        for name in ("subject", "body", "from_addr"):
            if name in fts_cols:
                fts_sel.append(name)
        fts = conn.execute(
            "SELECT %s FROM messages_fts WHERE id = ?" % ", ".join(fts_sel),
            (message_pk,),
        ).fetchone()
        if fts is not None:
            mapping = (
                dict(fts)
                if isinstance(fts, sqlite3.Row)
                else dict(zip(fts_sel, fts))
            )
            for key in ("subject", "body", "from_addr"):
                val = mapping.get(key)
                if val:
                    parts.append(str(val))
    return header, "\n".join(parts)


def backfill_identifiers(conn: sqlite3.Connection, *, limit: int | None = None) -> dict[str, int]:
    """Populate messages_ids.identifiers from live message + FTS text. Additive."""
    action = ensure_messages_ids(conn)
    if not table_exists(conn, "messages"):
        return {"action": action, "examined": 0, "updated": 0, "empty": 0}

    ids = [str(r[0]) for r in conn.execute("SELECT id FROM messages ORDER BY id")]
    if limit is not None:
        ids = ids[: max(0, int(limit))]
    updated = 0
    empty = 0
    for message_pk in ids:
        header, text = _row_text(conn, message_pk)
        tokens = extract_identifiers(message_pk, header, text)
        if not tokens and not header:
            empty += 1
            continue
        upsert_ids_row(
            conn,
            message_pk,
            message_id=header or "",
            identifiers=tokens,
        )
        updated += 1
    return {
        "action": action,
        "examined": len(ids),
        "updated": updated,
        "empty": empty,
    }


def match_messages_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = 50,
) -> list[dict]:
    """BM25 top-k on messages_ids (identifiers + message_id). Empty if unused."""
    if not table_exists(conn, "messages_ids"):
        return []
    terms = identifier_match_terms(query)
    if not terms:
        return []
    cols = set(columns(conn, "messages_ids"))
    if "identifiers" in cols:
        match_expr = " OR ".join(
            "{identifiers message_id} : %s" % term for term in terms
        )
    else:
        match_expr = " OR ".join(terms)
    sql = (
        "SELECT id AS message_id, bm25(messages_ids) AS bm25 "
        "FROM messages_ids WHERE messages_ids MATCH ? "
        "ORDER BY bm25(messages_ids) LIMIT ?"
    )
    try:
        rows = conn.execute(sql, (match_expr, int(k))).fetchall()
    except sqlite3.Error:
        try:
            rows = conn.execute(sql, (" OR ".join(terms), int(k))).fetchall()
        except sqlite3.Error:
            return []
    out: list[dict] = []
    for i, row in enumerate(rows, start=1):
        mid = row["message_id"] if isinstance(row, sqlite3.Row) else row[0]
        if not mid:
            continue
        out.append({"message_id": str(mid), "ids_rank": i})
    return out


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
        raise MessagesIdsError("refuse: will not backfill default SoR path under CI")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Additive messages_ids.identifiers backfill (APN / invoice / UUID). "
            "Does not rename columns. Does not apply SoR from CI."
        )
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SoR path (default: $MAILROOM_DB or ~/MailArchive/mailroom.sqlite).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Ensure identifiers column and populate from messages + FTS.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max messages to scan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.backfill:
        sys.stderr.write("error: pass --backfill (read-only retrieve does not write)\n")
        return 2
    db = Path(args.db).expanduser() if args.db else default_db_path()
    try:
        _ci_refuses_default_sor(db)
        if not db.is_file():
            raise MessagesIdsError("database not found: %s" % db)
        conn = sqlite3.connect(str(db))
        try:
            report = backfill_identifiers(conn, limit=args.limit)
            conn.commit()
        finally:
            conn.close()
    except MessagesIdsError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    sys.stdout.write(
        "messages_ids backfill action=%(action)s examined=%(examined)s "
        "updated=%(updated)s empty=%(empty)s\n" % report
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
