#!/usr/bin/env python3
"""Idempotent OpenAI embedding backfill into Mailroom sqlite-vec.

Reads bodies from messages_fts (join messages_fts.id = messages.id).
Always skips lane=auth unless --no-skip-auth. Never prints the API key.
Does not call IMAP. Does not rewrite FTS ingest.

Mac (Homebrew Python — Apple /usr/bin/python3 cannot load extensions):

  /opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dry-run
  /opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from embed_lib import (
    CHAR_CAP,
    DEFAULT_BATCH_SIZE,
    DEFAULT_DB,
    DEFAULT_DIMS,
    DEFAULT_MODEL,
    DEFAULT_MODEL_VERSION,
    EmbedError,
    apply_schema,
    backfill,
    connect_db,
    default_db_path,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Embed Mailroom FTS bodies with OpenAI text-embedding-3-small "
            f"({DEFAULT_DIMS}-d) into sqlite-vec. Skip lane=auth. Resume-safe."
        )
    )
    parser.add_argument(
        "--db",
        default=str(default_db_path()),
        help=f"Mailroom SQLite path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max messages to embed this run (resume-safe).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count/list candidates; no OpenAI call; still applies schema if missing.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI embedding model (default: {DEFAULT_MODEL}, {DEFAULT_DIMS}-d).",
    )
    parser.add_argument(
        "--model-version",
        default=DEFAULT_MODEL_VERSION,
        help=(
            f"Payload version stored in embedding_meta (default: {DEFAULT_MODEL_VERSION} = "
            f"first {CHAR_CAP} chars of subject+body)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"OpenAI embeddings batch size (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--skip-auth",
        dest="skip_auth",
        action="store_true",
        default=True,
        help="Skip lane=auth (default: on). Never embed 2FA/auth mail.",
    )
    parser.add_argument(
        "--no-skip-auth",
        dest="skip_auth",
        action="store_false",
        help="Do not skip lane=auth (not recommended).",
    )
    parser.add_argument(
        "--vec-extension",
        default=None,
        help="Path to vec0.dylib / vec0.so if pip sqlite-vec is not installed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db).expanduser()
    try:
        conn = connect_db(db, args.vec_extension)
        try:
            apply_schema(conn)
            backfill(
                conn,
                model=args.model,
                model_version=args.model_version,
                skip_auth=args.skip_auth,
                limit=args.limit,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
            )
        finally:
            conn.close()
    except EmbedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
