#!/usr/bin/env python3
"""Thin Mailroom helpers for CoS / local tools.

This repo does not ship `search_mail` (FTS exact-id stays on the Mac
Mailroom index). This module adds `semantic_search` in the same spirit:
return a list of dicts (id, subject, from_addr, score, snippet).

Install on the Mac:

  mkdir -p ~/MailArchive/scripts
  cp scripts/*.py scripts/*.sql scripts/README.md ~/MailArchive/scripts/

Then:

  from mailroom_tools import semantic_search
  hits = semantic_search("flight confirmation", db="~/MailArchive/mailroom.sqlite", k=10)
"""

from __future__ import annotations

from pathlib import Path

from embed_lib import (
    DEFAULT_DB,
    DEFAULT_DIMS,
    DEFAULT_K,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    EmbedError,
    semantic_search as _semantic_search,
)

# FTS search_mail is not in this repo. Exact-id lookup stays on messages_fts.
SEARCH_MAIL_NOTE = (
    "search_mail (FTS) is not shipped here. Use the Mac Mailroom FTS index "
    "for exact ids; semantic_search is cosine KNN only."
)


def semantic_search(
    query: str,
    db: str | Path = DEFAULT_DB,
    k: int = DEFAULT_K,
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    dims: int = DEFAULT_DIMS,
    extension_path: str | None = None,
    query_vector: list[float] | None = None,
) -> list[dict]:
    """Top-k semantic hits. Mirrors a search_mail-style list-of-dicts return.

    Keys: id, subject, from_addr, score (1 - cosine distance), distance, snippet.
    Embeds the query via local Ollama unless `query_vector` is supplied.
    """
    return _semantic_search(
        query,
        Path(db).expanduser(),
        k=k,
        model=model,
        ollama_url=ollama_url,
        dims=dims,
        extension_path=extension_path,
        query_vector=query_vector,
    )


__all__ = ["EmbedError", "SEARCH_MAIL_NOTE", "semantic_search"]
