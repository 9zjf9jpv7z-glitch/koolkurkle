#!/usr/bin/env python3
"""Query Mailroom sqlite-vec with an OpenAI-embedded search string.

Prints id, subject, from, score, snippet. FTS exact-id lookup stays on
messages_fts — this CLI does not replace FTS.

  /opt/homebrew/bin/python3 semantic_search.py --db ~/MailArchive/mailroom.sqlite 'receipt from apple'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from embed_lib import (
    DEFAULT_DB,
    DEFAULT_K,
    DEFAULT_MODEL,
    EmbedError,
    default_db_path,
    format_hits,
    semantic_search,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Semantic search over Mailroom embeddings (cosine via sqlite-vec). "
            "FTS remains separate — do not use this as an exact-id replacement."
        )
    )
    parser.add_argument("query", help="Natural-language query (embedded with --model).")
    parser.add_argument(
        "--db",
        default=str(default_db_path()),
        help=f"Mailroom SQLite path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=DEFAULT_K,
        help=f"Top-k neighbors (default: {DEFAULT_K}).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Must match the backfill model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON lines instead of the text table.",
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
        hits = semantic_search(
            args.query,
            db,
            k=args.k,
            model=args.model,
            extension_path=args.vec_extension,
        )
    except EmbedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not hits:
        print("no hits (backfill embeddings first; FTS exact-id search is separate)")
        return 0
    if args.json:
        for hit in hits:
            print(json.dumps(hit, ensure_ascii=False))
    else:
        print(format_hits(hits))
        print(
            "\n# FTS exact-id lookup remains on messages_fts — this is cosine KNN only.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
