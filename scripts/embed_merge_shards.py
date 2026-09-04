#!/usr/bin/env python3
"""Merge embedding rows from a secondary Mailroom DB copy into the primary.

Safe two-Mac pattern: the Mac mini embeds a *copy* of mailroom.sqlite for one
id-shard; this script copies only missing embedding_meta + message_embeddings
rows into the canonical primary DB on the MacBook Pro.

Missing-only: if primary already has embedding_meta for that
(message_id, model, model_version), the row is left alone (even if text_hash
differs). Never deletes primary rows. Never writes messages / FTS. Never
calls Ollama or IMAP.

    /opt/homebrew/bin/python3 embed_merge_shards.py \
      --primary-db ~/MailArchive/mailroom.sqlite \
      --secondary-db ~/MailArchive/mailroom-mini.sqlite

    /opt/homebrew/bin/python3 embed_merge_shards.py \
      --primary-db ~/MailArchive/mailroom.sqlite \
      --secondary-db ~/MailArchive/mailroom-mini.sqlite \
      --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from embed_lib import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VERSION,
    EmbedError,
    connect_db,
    merge_shards,
    model_id,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copy missing sqlite-vec embed rows from a secondary DB copy "
            "into the canonical primary. Missing-only, idempotent, no Ollama. "
            f"Default model {DEFAULT_MODEL} (stored {DEFAULT_MODEL_ID})."
        )
    )
    parser.add_argument(
        "--primary-db",
        required=True,
        help="Canonical Mailroom SQLite (MBP). New embed rows are inserted here.",
    )
    parser.add_argument(
        "--secondary-db",
        required=True,
        help="Writable copy the mini (or other shard) embedded into.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Must match both backfills (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--model-version",
        default=DEFAULT_MODEL_VERSION,
        help=f"Payload version in embedding_meta (default: {DEFAULT_MODEL_VERSION}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count would-insert / skip; do not write the primary.",
    )
    parser.add_argument(
        "--vec-extension",
        default=None,
        help="Path to vec0.dylib / vec0.so if pip sqlite-vec is not installed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    primary_path = Path(args.primary_db).expanduser()
    secondary_path = Path(args.secondary_db).expanduser()
    if not primary_path.is_file():
        print(f"error: primary DB not found: {primary_path}", file=sys.stderr)
        return 2
    if not secondary_path.is_file():
        print(f"error: secondary DB not found: {secondary_path}", file=sys.stderr)
        return 2
    if primary_path.resolve() == secondary_path.resolve():
        print("error: --primary-db and --secondary-db must be different files", file=sys.stderr)
        return 2
    try:
        primary = connect_db(primary_path, args.vec_extension)
        secondary = connect_db(secondary_path, args.vec_extension)
        try:
            counts = merge_shards(
                primary,
                secondary,
                model=args.model,
                model_version=args.model_version,
                dry_run=args.dry_run,
            )
        finally:
            primary.close()
            secondary.close()
    except EmbedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        "examined={examined} inserted={inserted} "
        "skipped_already_present={skipped_already_present} "
        "missing_vector={missing_vector} errors={errors} "
        "model={model} version={version}{dry}".format(
            model=model_id(args.model),
            version=args.model_version,
            dry=" dry-run" if args.dry_run else "",
            **counts,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
