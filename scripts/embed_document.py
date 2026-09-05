#!/usr/bin/env python3
"""Header-prefixed document payload (MAILROOM.md §6.1 / §11.1).

Subject: {subject}
From: {from}
To: {to}
Date: {date_iso}
Lane: {lane}

{cleaned_new_body}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

INSTRUCT_VERSION = "v1"
EMBED_MODEL_TAG = "qwen3-embedding:8b"
EMBED_DIM = 1024

HEADER_KEYS = ("Subject", "From", "To", "Date", "Lane")


def _s(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def format_date_iso(value: Any) -> str:
    """Prefer an ISO-8601 UTC date. Pass through already-ISO strings."""
    text = _s(value)
    if not text:
        return ""
    probe = text
    if probe.endswith("Z"):
        probe = probe[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(probe)
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def format_from(from_addr: Any = None, from_name: Any = None) -> str:
    addr = _s(from_addr)
    name = _s(from_name)
    if name and addr:
        if name.lower() == addr.lower():
            return addr
        if "<" in addr:
            return addr if name.lower() in addr.lower() else "%s %s" % (name, addr)
        return "%s <%s>" % (name, addr)
    return addr or name


def document_embed_text(
    *,
    subject: Any = None,
    from_addr: Any = None,
    from_name: Any = None,
    to_addrs: Any = None,
    date_iso: Any = None,
    lane: Any = None,
    cleaned_body: Any = None,
    cap: int | None = None,
) -> str:
    """Build the §6.1 document. Optional ``cap`` truncates the finished text."""
    header = "\n".join(
        [
            "Subject: %s" % _s(subject),
            "From: %s" % format_from(from_addr, from_name),
            "To: %s" % _s(to_addrs),
            "Date: %s" % format_date_iso(date_iso),
            "Lane: %s" % _s(lane),
        ]
    )
    body = _s(cleaned_body)
    raw = "%s\n\n%s" % (header, body) if body else header
    if cap is not None and cap >= 0 and len(raw) > cap:
        return raw[:cap]
    return raw
