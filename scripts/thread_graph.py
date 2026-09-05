#!/usr/bin/env python3
"""Thread-graph fields from RFC 5322 headers (MAILROOM.md §6.1).

thread_id is the conversation root: first References token, else In-Reply-To,
else this message's Message-ID, else messages.id.
"""

from __future__ import annotations

import re
from typing import Any

_MSG_ID = re.compile(r"<[^<>\s]+>")


def message_id_tokens(header: str | None) -> list[str]:
    """Return ``<id@host>`` tokens in header order (no dedupe)."""
    if not header:
        return []
    return _MSG_ID.findall(header)


def first_message_id(header: str | None) -> str | None:
    tokens = message_id_tokens(header)
    return tokens[0] if tokens else None


def normalize_message_id(value: str | None) -> str | None:
    """Keep a bare id or wrap a token-looking value. Empty → None."""
    text = (value or "").strip()
    if not text:
        return None
    tokens = message_id_tokens(text)
    if tokens:
        return tokens[0]
    if "@" in text and " " not in text:
        if text.startswith("<") and text.endswith(">"):
            return text
        return "<%s>" % text.strip("<>")
    return text


def thread_fields(
    *,
    message_id: str | None = None,
    message_id_header: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
) -> dict[str, str | None]:
    """Build thread_id / in_reply_to / references_header for ``messages``."""
    refs_raw = (references_header or "").strip() or None
    refs = message_id_tokens(refs_raw)
    irt = normalize_message_id(in_reply_to)
    own = first_message_id(message_id_header) or normalize_message_id(message_id_header)
    row_id = (message_id or "").strip() or None

    if refs:
        thread_id = refs[0]
    elif irt:
        thread_id = irt
    elif own:
        thread_id = own
    else:
        thread_id = row_id

    return {
        "thread_id": thread_id,
        "in_reply_to": irt,
        "references_header": refs_raw,
    }


def thread_fields_from_row(row: Any) -> dict[str, str | None]:
    """Accept a mapping / sqlite Row with live + PR-1 column names."""
    get = row.get if hasattr(row, "get") else None

    def _get(key: str) -> str | None:
        if get is not None:
            try:
                val = get(key)
            except Exception:
                val = None
        else:
            try:
                val = row[key]
            except (KeyError, IndexError, TypeError):
                val = None
        if val is None:
            return None
        return str(val)

    return thread_fields(
        message_id=_get("id"),
        message_id_header=_get("message_id_header"),
        in_reply_to=_get("in_reply_to"),
        references_header=_get("references_header"),
    )
