#!/usr/bin/env python3
"""Quote-strip + signature-strip for MAILROOM.md §6.1 cleaned_body.

Keeps the raw body untouched. content_hash is SHA-256 of the cleaned
new-body (UTF-8). Used by the incremental embed path only.
"""

from __future__ import annotations

import hashlib
import re

# Outlook / Apple / Gmail reply split: from this line to EOF is quoted history.
_SPLIT_RES = (
    re.compile(r"^On .{0,240} wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^_{5,}\s*$", re.MULTILINE),
    re.compile(r"^Begin forwarded message:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"^From:\s.+\n(?:Sent|Date):\s.+\n(?:To:\s.+\n)?(?:Subject:\s)",
        re.IGNORECASE | re.MULTILINE,
    ),
)

# RFC 3676 signature delimiter and common mobile/client footers.
_SIG_RES = (
    re.compile(r"^-- \s*$", re.MULTILINE),
    re.compile(r"^Sent from my iPhone.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Sent from my iPad.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Get Outlook for .*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Sent from Mail for Windows.*$", re.IGNORECASE | re.MULTILINE),
)

_QUOTE_LINE = re.compile(r"^>+")


def normalize_newlines(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _cut_at_first(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    cut = None
    for pat in patterns:
        match = pat.search(text)
        if match is None:
            continue
        if cut is None or match.start() < cut:
            cut = match.start()
    if cut is None:
        return text
    return text[:cut]


def _drop_quoted_lines(text: str) -> str:
    kept: list[str] = []
    for line in text.split("\n"):
        if _QUOTE_LINE.match(line.lstrip(" \t")):
            continue
        kept.append(line)
    return "\n".join(kept)


def strip_quotes(text: str | None) -> str:
    """Drop forwarded/replied history. Leaves the new-body fragment."""
    body = normalize_newlines(text)
    body = _cut_at_first(body, _SPLIT_RES)
    body = _drop_quoted_lines(body)
    return body


def strip_signature(text: str | None) -> str:
    """Drop RFC 3676 ``-- `` and common client signature footers."""
    body = normalize_newlines(text)
    return _cut_at_first(body, _SIG_RES)


def clean_body(raw_body: str | None) -> str:
    """Quote-strip then signature-strip. Trailing whitespace collapsed."""
    cleaned = strip_signature(strip_quotes(raw_body))
    cleaned = cleaned.strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def content_hash(cleaned: str | None) -> str:
    """SHA-256 hex of cleaned_body (UTF-8). Empty string hashes as empty."""
    return hashlib.sha256((cleaned or "").encode("utf-8")).hexdigest()
