#!/usr/bin/env python3
"""Shared Mailroom embedding + sqlite-vec helpers.

Copy this file next to embed_backfill.py / semantic_search.py /
mailroom_tools.py (repo `scripts/` or `~/MailArchive/scripts/`).

Never logs API keys. No IMAP. Does not rewrite FTS ingest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

DEFAULT_DB = Path.home() / "MailArchive" / "mailroom.sqlite"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_MODEL_VERSION = "v1"
DEFAULT_DIMS = 1536
DEFAULT_K = 10
DEFAULT_BATCH_SIZE = 64
# v1: first N chars of `subject + "\\n\\n" + body`. ~6k tokens at 4 chars/token.
# text-embedding-3-small max input is 8191 tokens; 24000 chars stays under that.
CHAR_CAP = 24000
SNIPPET_CHARS = 180
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

SCHEMA_PATH = Path(__file__).resolve().parent / "embed_schema.sql"

# Preferred first. Scripts try each pair; fail clearly if none exist.
# `security … -w` must be last (macOS Tahoe).
KEYCHAIN_CANDIDATES = (
    ("openai-api-key", "koolkurkle"),
    ("OpenAI API Key", "koolkurkle"),
    ("OpenAI", "api-key"),
    ("openai", "OPENAI_API_KEY"),
    ("com.openai.api", "koolkurkle"),
)

# Homebrew / release dylibs on Apple Silicon, then Intel.
VEC_EXTENSION_CANDIDATES = (
    "/opt/homebrew/opt/sqlite-vec/lib/vec0.dylib",
    "/opt/homebrew/lib/vec0.dylib",
    str(Path.home() / "MailArchive" / "lib" / "vec0.dylib"),
    "/usr/local/opt/sqlite-vec/lib/vec0.dylib",
    "/usr/local/lib/vec0.dylib",
)

EmbedFn = Callable[[Sequence[str], str, str], list[list[float]]]


class EmbedError(RuntimeError):
    """Embedding or sqlite-vec failure (never includes secrets)."""


def default_db_path() -> Path:
    return Path(DEFAULT_DB)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def serialize_f32(vector: Sequence[float]) -> bytes:
    return struct.pack("%sf" % len(vector), *vector)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_text(subject: str | None, body: str | None, cap: int = CHAR_CAP) -> str:
    """v1 payload: first `cap` chars of subject + blank line + FTS body."""
    subj = (subject or "").strip()
    body_text = (body or "").strip()
    if subj and body_text:
        raw = f"{subj}\n\n{body_text}"
    else:
        raw = subj or body_text
    if len(raw) > cap:
        return raw[:cap]
    return raw


def snippet(body: str | None, n: int = SNIPPET_CHARS) -> str:
    collapsed = " ".join((body or "").split())
    if len(collapsed) <= n:
        return collapsed
    return collapsed[: n - 1] + "…"


def _redact_headers(headers: dict) -> dict:
    out = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "api-key", "x-api-key"}:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


def read_openai_api_key(
    *,
    allow_env: bool = True,
    security_bin: str = "security",
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    """Key from OPENAI_API_KEY (tests/override) or macOS Keychain.

    Never prints or logs the secret. `security find-generic-password … -w`
    keeps `-w` last (Tahoe).
    """
    if allow_env:
        env = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if env:
            return env

    runner = run or subprocess.run
    tried = []
    for service, account in KEYCHAIN_CANDIDATES:
        tried.append(f"service={service!r} account={account!r}")
        cmd = [
            security_bin,
            "find-generic-password",
            "-s",
            service,
            "-a",
            account,
            "-w",
        ]
        try:
            result = runner(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise EmbedError(
                "macOS `security` not found. On this Mac, add the key with "
                "`security add-generic-password` (see scripts/README.md) or set "
                "OPENAI_API_KEY for a one-off test."
            ) from exc
        if result.returncode == 0:
            key = (result.stdout or "").strip()
            if key:
                return key
    names = "; ".join(tried)
    raise EmbedError(
        "OpenAI API key not found in the environment or Keychain. "
        "Preferred item: service=openai-api-key account=koolkurkle. "
        f"Also tried: {names}. "
        "Add it with `security add-generic-password -a koolkurkle "
        "-s openai-api-key -w` (`-w` must be last on Tahoe), "
        "or set OPENAI_API_KEY for tests only."
    )


def openai_embed_batch(
    texts: Sequence[str],
    model: str,
    api_key: str,
    *,
    url: str = OPENAI_EMBEDDINGS_URL,
    opener: Callable[..., object] | None = None,
    timeout: int = 120,
) -> list[list[float]]:
    """POST /v1/embeddings via stdlib urllib. Does not log the API key."""
    if not texts:
        return []
    payload = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise EmbedError(
            f"OpenAI embeddings HTTP {exc.code}: {body} "
            f"(headers={_redact_headers(dict(request.headers))})"
        ) from None
    except urllib.error.URLError as exc:
        raise EmbedError(f"OpenAI embeddings network error: {exc.reason!r}") from None
    data = json.loads(raw.decode("utf-8"))
    items = data.get("data")
    if not isinstance(items, list) or len(items) != len(texts):
        raise EmbedError("OpenAI embeddings response missing data[] of expected length.")
    items = sorted(items, key=lambda row: int(row.get("index", 0)))
    vectors = []
    for row in items:
        vec = row.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbedError("OpenAI embeddings response row missing embedding.")
        vectors.append([float(x) for x in vec])
    return vectors


def _try_load_path(conn: sqlite3.Connection, path: str) -> bool:
    if not path or not Path(path).is_file():
        return False
    conn.load_extension(path)
    return True


def load_sqlite_vec(conn: sqlite3.Connection, extension_path: str | None = None) -> str:
    """Load sqlite-vec. Prefer pip `sqlite_vec.load`, then a .dylib path."""
    if not hasattr(conn, "enable_load_extension"):
        raise EmbedError(
            "This Python's SQLite cannot load extensions "
            "(typical of Apple /usr/bin/python3). Use Homebrew Python: "
            "brew install python && /opt/homebrew/bin/python3 …"
        )
    conn.enable_load_extension(True)
    loaded = None
    errors = []
    explicit = extension_path or os.environ.get("SQLITE_VEC_EXTENSION") or ""
    if explicit:
        try:
            if _try_load_path(conn, explicit):
                loaded = explicit
            else:
                conn.load_extension(explicit)
                loaded = explicit
        except Exception as exc:  # noqa: BLE001 — surface a clear path error
            errors.append(f"{explicit}: {exc}")
    if loaded is None:
        try:
            import sqlite_vec  # type: ignore

            sqlite_vec.load(conn)
            loaded = "sqlite_vec.load"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pip sqlite-vec: {exc}")
    if loaded is None:
        for path in VEC_EXTENSION_CANDIDATES:
            try:
                if _try_load_path(conn, path):
                    loaded = path
                    break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
    try:
        conn.enable_load_extension(False)
    except Exception:
        pass
    if loaded is None:
        detail = "; ".join(errors) if errors else "no loader succeeded"
        raise EmbedError(
            "Could not load sqlite-vec. On macOS arm64: "
            "brew install python && /opt/homebrew/bin/python3 -m pip install sqlite-vec "
            "(Apple /usr/bin/python3 cannot load extensions). "
            "Or pass --vec-extension /path/to/vec0.dylib from "
            "https://github.com/asg017/sqlite-vec/releases (macos-aarch64). "
            f"Tried: {detail}"
        )
    return loaded


def connect_db(db_path: str | Path, extension_path: str | None = None) -> sqlite3.Connection:
    path = Path(db_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    load_sqlite_vec(conn, extension_path)
    return conn


def apply_schema(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    sql = schema_sql
    if sql is None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def ensure_mailroom_tables(conn: sqlite3.Connection) -> None:
    """Create the existing Mailroom messages + FTS tables if missing (tests)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          source TEXT,
          folder TEXT,
          lane TEXT,
          junk INTEGER,
          uid INTEGER,
          present_on_server INTEGER
        )
        """
    )
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if row is None:
        conn.execute(
            """
            CREATE VIRTUAL TABLE messages_fts USING fts5(
              id UNINDEXED,
              subject,
              body,
              from_addr,
              tokenize='porter unicode61'
            )
            """
        )
    conn.commit()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return row is not None


def candidate_counts(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
) -> dict[str, int]:
    """Dry-run stats: auth / empty-body / already-embedded / hash-changed / due."""
    if not _has_table(conn, "messages") or not _has_table(conn, "messages_fts"):
        raise EmbedError(
            "DB is missing messages / messages_fts. This script does not ingest mail."
        )
    has_meta = _has_table(conn, "embedding_meta")
    auth_clause = "LOWER(COALESCE(m.lane, '')) = 'auth'"
    empty_clause = "TRIM(COALESCE(f.body, '')) = ''"
    total = conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN messages_fts f ON f.id = m.id"
    ).fetchone()[0]
    auth = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {auth_clause}
        """
    ).fetchone()[0]
    empty = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {empty_clause}
          AND (? = 0 OR NOT ({auth_clause}))
        """,
        (1 if skip_auth else 0,),
    ).fetchone()[0]
    already = 0
    hash_changed = 0
    if has_meta:
        rows = conn.execute(
            """
            SELECT m.id, f.subject, f.body, e.text_hash
            FROM messages m
            JOIN messages_fts f ON f.id = m.id
            JOIN embedding_meta e
              ON e.message_id = m.id AND e.model = ? AND e.model_version = ?
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
            """,
            (model, model_version, 1 if skip_auth else 0),
        )
        for row in rows:
            payload = embed_text(row["subject"], row["body"])
            if sha256_text(payload) == row["text_hash"]:
                already += 1
            else:
                hash_changed += 1
    if has_meta:
        due = conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN messages_fts f ON f.id = m.id
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
              AND NOT EXISTS (
                SELECT 1 FROM embedding_meta e
                WHERE e.message_id = m.id AND e.model = ? AND e.model_version = ?
              )
            """,
            (1 if skip_auth else 0, model, model_version),
        ).fetchone()[0]
    else:
        due = conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN messages_fts f ON f.id = m.id
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
            """,
            (1 if skip_auth else 0,),
        ).fetchone()[0]
    return {
        "joined": int(total),
        "skipped_auth": int(auth) if skip_auth else 0,
        "skipped_empty_body": int(empty),
        "skipped_already_embedded": int(already),
        "reembed_hash_changed": int(hash_changed),
        "candidates": int(due) + int(hash_changed),
    }


def iter_candidates(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Messages with a non-empty FTS body, not auth, needing (re)embed."""
    if not _has_table(conn, "messages") or not _has_table(conn, "messages_fts"):
        raise EmbedError(
            "DB is missing messages / messages_fts. This script does not ingest mail."
        )
    sql = """
        SELECT m.id AS id, m.source AS source, m.lane AS lane,
               f.subject AS subject, f.body AS body, f.from_addr AS from_addr,
               e.text_hash AS existing_hash
        FROM messages m
        JOIN messages_fts f ON f.id = m.id
        LEFT JOIN embedding_meta e
          ON e.message_id = m.id AND e.model = ? AND e.model_version = ?
        WHERE TRIM(COALESCE(f.body, '')) != ''
          AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
        ORDER BY m.id
    """
    params: list = [model, model_version, 1 if skip_auth else 0]
    if not _has_table(conn, "embedding_meta"):
        sql = """
            SELECT m.id AS id, m.source AS source, m.lane AS lane,
                   f.subject AS subject, f.body AS body, f.from_addr AS from_addr,
                   NULL AS existing_hash
            FROM messages m
            JOIN messages_fts f ON f.id = m.id
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
            ORDER BY m.id
        """
        params = [1 if skip_auth else 0]
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        payload = embed_text(row["subject"], row["body"])
        digest = sha256_text(payload)
        existing = row["existing_hash"]
        if existing and existing == digest:
            continue
        out.append(
            {
                "id": row["id"],
                "source": row["source"],
                "lane": row["lane"],
                "subject": row["subject"] or "",
                "body": row["body"] or "",
                "from_addr": row["from_addr"] or "",
                "text": payload,
                "text_hash": digest,
                "reembed": bool(existing),
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def upsert_embedding(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    vector: Sequence[float],
    model: str,
    model_version: str,
    text_hash: str,
    char_count: int,
    created_at: str | None = None,
) -> None:
    if len(vector) != DEFAULT_DIMS:
        raise EmbedError(
            f"Embedding dim {len(vector)} != {DEFAULT_DIMS} "
            f"(text-embedding-3-small v1 is {DEFAULT_DIMS}-d)."
        )
    created = created_at or utc_now()
    blob = serialize_f32(vector)
    conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (message_id,))
    conn.execute(
        "INSERT INTO message_embeddings(message_id, embedding) VALUES (?, ?)",
        (message_id, blob),
    )
    conn.execute(
        """
        INSERT INTO embedding_meta(
          message_id, model, model_version, created_at, text_hash, char_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id, model, model_version) DO UPDATE SET
          created_at = excluded.created_at,
          text_hash = excluded.text_hash,
          char_count = excluded.char_count
        """,
        (message_id, model, model_version, created, text_hash, char_count),
    )


def backfill(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    api_key: str | None = None,
    embed_fn: EmbedFn | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Idempotent embed of FTS-backed messages. Resume-safe (commit per batch)."""
    apply_schema(conn)
    counts = candidate_counts(
        conn, model=model, model_version=model_version, skip_auth=skip_auth
    )
    emit = log or (lambda msg: print(msg, file=sys.stderr))
    emit(
        "backfill {c} candidate(s); skipped_auth={a} skipped_empty_body={e} "
        "skipped_already_embedded={s} reembed_hash_changed={h} joined={j}".format(
            c=counts["candidates"],
            a=counts["skipped_auth"],
            e=counts["skipped_empty_body"],
            s=counts["skipped_already_embedded"],
            h=counts["reembed_hash_changed"],
            j=counts["joined"],
        )
    )
    rows = iter_candidates(
        conn,
        model=model,
        model_version=model_version,
        skip_auth=skip_auth,
        limit=limit,
    )
    if dry_run:
        emit(f"dry-run: would embed {len(rows)} row(s) this pass (limit={limit!r})")
        for row in rows[:20]:
            flag = "reembed" if row["reembed"] else "new"
            emit(
                f"  {row['id']}  [{flag}] source={row['source']!r} "
                f"lane={row['lane']!r} chars={len(row['text'])}"
            )
        if len(rows) > 20:
            emit(f"  … {len(rows) - 20} more")
        counts["would_embed"] = len(rows)
        counts["embedded"] = 0
        return counts

    if not rows:
        counts["embedded"] = 0
        return counts

    key = api_key
    worker = embed_fn
    if worker is None:
        if not key:
            key = read_openai_api_key()
        worker = lambda texts, mdl, _k=key: openai_embed_batch(texts, mdl, _k)

    embedded = 0
    size = max(1, int(batch_size))
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        texts = [row["text"] for row in batch]
        emit(
            f"embedding batch {start // size + 1} "
            f"({len(batch)} msgs, {start + 1}-{start + len(batch)} of {len(rows)})"
        )
        vectors = worker(texts, model)
        if len(vectors) != len(batch):
            raise EmbedError("embed function returned the wrong number of vectors")
        for row, vector in zip(batch, vectors):
            upsert_embedding(
                conn,
                message_id=row["id"],
                vector=vector,
                model=model,
                model_version=model_version,
                text_hash=row["text_hash"],
                char_count=len(row["text"]),
            )
            embedded += 1
        conn.commit()
        emit(f"committed {embedded}/{len(rows)}")
    counts["embedded"] = embedded
    counts["would_embed"] = 0
    return counts


def knn_search(
    conn: sqlite3.Connection,
    query_vector: Sequence[float],
    *,
    k: int = DEFAULT_K,
) -> list[dict]:
    if len(query_vector) != DEFAULT_DIMS:
        raise EmbedError(
            f"Query vector dim {len(query_vector)} != {DEFAULT_DIMS}."
        )
    if not _has_table(conn, "message_embeddings"):
        raise EmbedError("message_embeddings is missing. Run embed_backfill.py first.")
    k = max(1, int(k))
    rows = conn.execute(
        """
        SELECT
          v.message_id AS id,
          v.distance AS distance,
          f.subject AS subject,
          f.from_addr AS from_addr,
          f.body AS body
        FROM message_embeddings AS v
        LEFT JOIN messages_fts AS f ON f.id = v.message_id
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (serialize_f32(query_vector), k),
    ).fetchall()
    results = []
    for row in rows:
        distance = float(row["distance"])
        results.append(
            {
                "id": row["id"],
                "subject": row["subject"] or "",
                "from_addr": row["from_addr"] or "",
                "score": 1.0 - distance,
                "distance": distance,
                "snippet": snippet(row["body"]),
            }
        )
    return results


def semantic_search(
    query: str,
    db: str | Path,
    *,
    k: int = DEFAULT_K,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    embed_fn: EmbedFn | None = None,
    extension_path: str | None = None,
    query_vector: Sequence[float] | None = None,
) -> list[dict]:
    """Embed `query` (or use `query_vector`) and return top-k cosine hits.

    FTS exact-id lookup stays on messages_fts — this does not replace it.
    """
    q = (query or "").strip()
    if not q and query_vector is None:
        raise EmbedError("Query text is empty.")
    conn = connect_db(db, extension_path)
    try:
        apply_schema(conn)
        vector: Sequence[float]
        if query_vector is not None:
            vector = query_vector
        else:
            worker = embed_fn
            key = api_key
            if worker is None:
                if not key:
                    key = read_openai_api_key()
                worker = lambda texts, mdl, _k=key: openai_embed_batch(texts, mdl, _k)
            vectors = worker([q], model)
            if not vectors:
                raise EmbedError("Embedding the query returned no vector.")
            vector = vectors[0]
        return knn_search(conn, vector, k=k)
    finally:
        conn.close()


def format_hits(hits: Iterable[dict]) -> str:
    lines = []
    for i, hit in enumerate(hits, 1):
        lines.append(
            f"{i}. {hit['id']}  score={hit['score']:.4f}  "
            f"distance={hit['distance']:.4f}  from={hit['from_addr']}"
        )
        lines.append(f"   subject: {hit['subject']}")
        lines.append(f"   snippet: {hit['snippet']}")
    return "\n".join(lines)
