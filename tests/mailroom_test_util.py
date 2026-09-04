"""Shared fixtures for Mailroom embed tests. No network."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402

DIMS = el.DEFAULT_DIMS


def one_hot(index: int, dims: int = DIMS) -> list[float]:
    vec = [0.0] * dims
    vec[index % dims] = 1.0
    return vec


def open_mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    el.load_sqlite_vec(conn)
    el.ensure_mailroom_tables(conn)
    el.apply_schema(conn)
    return conn


def insert_message(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    subject: str = "Hello",
    body: str = "Hi there",
    from_addr: str = "Ada <ada@example.com>",
    source: str = "dump",
    lane: str = "inbox",
) -> None:
    conn.execute(
        """
        INSERT INTO messages(id, source, folder, lane, junk, uid, present_on_server)
        VALUES (?, ?, 'INBOX', ?, 0, 1, 1)
        """,
        (message_id, source, lane),
    )
    conn.execute(
        """
        INSERT INTO messages_fts(id, subject, body, from_addr)
        VALUES (?, ?, ?, ?)
        """,
        (message_id, subject, body, from_addr),
    )
    conn.commit()


def fake_embed_fn(mapping: dict[str, list[float]] | None = None):
    """Return vectors without calling Ollama. Default: one-hot from text hash."""

    def _embed(texts, model):
        out = []
        for text in texts:
            if mapping and text in mapping:
                out.append(mapping[text])
                continue
            idx = int(el.sha256_text(text)[:8], 16) % DIMS
            out.append(one_hot(idx))
        return out

    return _embed


class FakeHTTPResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._raw = json.dumps(payload).encode()
        self.status = status
        self.code = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
