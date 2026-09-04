#!/usr/bin/env python3
"""Shared Mailroom embedding + sqlite-vec helpers.

Local Ollama Qwen3-Embedding-8B (no OpenAI). Copy this file next to
embed_backfill.py / semantic_search.py / mailroom_tools.py (repo
`scripts/` or `~/MailArchive/scripts/`).

Never calls a cloud API. No IMAP. Does not rewrite FTS ingest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

DEFAULT_DB = Path.home() / "MailArchive" / "mailroom.sqlite"
# Official Ollama library tag: https://ollama.com/library/qwen3-embedding:8b
# (`qwen3-embedding` / `qwen3-embedding:latest` is the same 8B Q4_K_M).
DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_MODEL_ID = "qwen3-embedding-8b"
DEFAULT_MODEL_VERSION = "v1"
# Native Qwen3-Embedding-8B output is 4096-d. v1 stores a Matryoshka
# prefix of 1024-d (L2-renormalized) so each row is 4 KiB instead of 16 KiB.
# The model is trained for this; see scripts/README.md.
NATIVE_DIMS = 4096
DEFAULT_DIMS = 1024
DEFAULT_K = 10
# Local 8B on Apple Silicon: small batches keep Metal/RAM steady.
DEFAULT_BATCH_SIZE = 8
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# v1: first N chars of `subject + "\\n\\n" + body`. ~4k tokens at 4 chars/token,
# well under the model's 32k context, and faster for local 8B backfill.
CHAR_CAP = 16000
SNIPPET_CHARS = 180
# Qwen3-Embedding retrieval: instruct prefix on *queries* only (not documents).
QUERY_INSTRUCT = (
    "Instruct: Given a mail search query, retrieve the most relevant email.\n"
    "Query: "
)

SCHEMA_PATH = Path(__file__).resolve().parent / "embed_schema.sql"

# Homebrew / release dylibs on Apple Silicon, then Intel.
VEC_EXTENSION_CANDIDATES = (
    "/opt/homebrew/opt/sqlite-vec/lib/vec0.dylib",
    "/opt/homebrew/lib/vec0.dylib",
    str(Path.home() / "MailArchive" / "lib" / "vec0.dylib"),
    "/usr/local/opt/sqlite-vec/lib/vec0.dylib",
    "/usr/local/lib/vec0.dylib",
)

# Official 8B aliases collapse to one stored id so pull-tag variants match.
_QWEN3_8B_ALIASES = frozenset(
    {
        "qwen3-embedding:8b",
        "qwen3-embedding",
        "qwen3-embedding:latest",
        "qwen3-embedding-8b",
    }
)

EmbedFn = Callable[[Sequence[str], str], list[list[float]]]


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


def model_id(model: str | None) -> str:
    """Stable `embedding_meta.model`. Official 8B Ollama tags → qwen3-embedding-8b."""
    tag = (model or DEFAULT_MODEL).strip().lower()
    if tag in _QWEN3_8B_ALIASES:
        return DEFAULT_MODEL_ID
    return tag.replace(":", "-")


def ollama_model_tag(model: str | None) -> str:
    """Tag sent to Ollama. Stored id `qwen3-embedding-8b` maps back to `:8b`."""
    tag = (model or DEFAULT_MODEL).strip()
    if not tag:
        return DEFAULT_MODEL
    if tag.lower() == DEFAULT_MODEL_ID:
        return DEFAULT_MODEL
    return tag


def embed_text(subject: str | None, body: str | None, cap: int = CHAR_CAP) -> str:
    """v1 document payload: first `cap` chars of subject + blank line + FTS body."""
    subj = (subject or "").strip()
    body_text = (body or "").strip()
    if subj and body_text:
        raw = f"{subj}\n\n{body_text}"
    else:
        raw = subj or body_text
    if len(raw) > cap:
        return raw[:cap]
    return raw


def query_text(query: str) -> str:
    """v1 search payload: Qwen3 instruct prefix + stripped query."""
    return QUERY_INSTRUCT + (query or "").strip()


def snippet(body: str | None, n: int = SNIPPET_CHARS) -> str:
    collapsed = " ".join((body or "").split())
    if len(collapsed) <= n:
        return collapsed
    return collapsed[: n - 1] + "…"


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm == 0.0:
        raise EmbedError(
            "Ollama returned a zero embedding. Pull/update the model: "
            f"`ollama pull {DEFAULT_MODEL}` (and prefer POST /api/embed, not "
            "the deprecated /api/embeddings)."
        )
    return [float(x) / norm for x in vector]


def adapt_dims(vector: Sequence[float], dims: int) -> list[float]:
    """Keep native length, or Matryoshka-truncate + L2-renormalize."""
    if dims <= 0:
        raise EmbedError(f"dims must be > 0, got {dims}")
    if len(vector) == dims:
        return [float(x) for x in vector]
    if len(vector) < dims:
        raise EmbedError(
            f"Embedding dim {len(vector)} < requested {dims}. "
            f"{DEFAULT_MODEL} native is {NATIVE_DIMS}."
        )
    return l2_normalize(vector[:dims])


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _post_json(
    url: str,
    payload: dict,
    *,
    opener: Callable[..., object] | None,
    timeout: int,
) -> tuple[int, bytes]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or getattr(resp, "code", 200)
            return int(status), raw
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise EmbedError(f"Ollama embeddings HTTP {exc.code} at {url}: {err_body}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise EmbedError(
            f"Cannot reach Ollama at {url} ({reason!r}). "
            f"Start it locally (`brew services start ollama` or `ollama serve`) "
            f"and pull the model once: `ollama pull {DEFAULT_MODEL}`. "
            "After the pull, backfill and search stay offline."
        ) from None


def _parse_ollama_native(data: object, n_texts: int) -> list[list[float]] | None:
    if not isinstance(data, dict):
        return None
    embeddings = data.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        if len(embeddings) != n_texts:
            raise EmbedError(
                f"Ollama /api/embed returned {len(embeddings)} vectors, expected {n_texts}."
            )
        out = []
        for row in embeddings:
            if not isinstance(row, list) or not row:
                raise EmbedError("Ollama /api/embed row missing embedding.")
            out.append([float(x) for x in row])
        return out
    single = data.get("embedding")
    if isinstance(single, list) and single and n_texts == 1:
        return [[float(x) for x in single]]
    return None


def _parse_openai_compat(data: object, n_texts: int) -> list[list[float]] | None:
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return None
    if len(items) != n_texts:
        raise EmbedError(
            f"Ollama /v1/embeddings returned {len(items)} vectors, expected {n_texts}."
        )
    items = sorted(items, key=lambda row: int(row.get("index", 0)))
    out = []
    for row in items:
        vec = row.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbedError("Ollama /v1/embeddings row missing embedding.")
        out.append([float(x) for x in vec])
    return out


def ollama_embed_batch(
    texts: Sequence[str],
    model: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    dims: int = DEFAULT_DIMS,
    opener: Callable[..., object] | None = None,
    timeout: int = 300,
) -> list[list[float]]:
    """Embed via local Ollama. Prefers POST /api/embed; falls back to /v1/embeddings.

    No API key. Does not call api.openai.com. After `ollama pull`, this is offline.
    """
    if not texts:
        return []
    tag = ollama_model_tag(model)
    base = ollama_url.rstrip("/")
    native_payload = {
        "model": tag,
        "input": list(texts),
        "keep_alive": "30m",
        "dimensions": int(dims),
    }
    raw = None
    parsed = None
    try:
        _status, raw = _post_json(
            _join_url(base, "/api/embed"),
            native_payload,
            opener=opener,
            timeout=timeout,
        )
        parsed = _parse_ollama_native(json.loads(raw.decode("utf-8")), len(texts))
    except EmbedError as exc:
        msg = str(exc)
        # 404: older builds only expose the OpenAI-compatible route.
        if "HTTP 404" not in msg:
            raise
        parsed = None
    if parsed is None:
        compat_payload = {
            "model": tag,
            "input": list(texts),
            "dimensions": int(dims),
        }
        _status, raw = _post_json(
            _join_url(base, "/v1/embeddings"),
            compat_payload,
            opener=opener,
            timeout=timeout,
        )
        parsed = _parse_openai_compat(json.loads(raw.decode("utf-8")), len(texts))
    if parsed is None:
        raise EmbedError(
            "Ollama embeddings response missing embeddings[] / data[]. "
            f"Is `{tag}` pulled? `ollama pull {DEFAULT_MODEL}`"
        )
    return [adapt_dims(vec, dims) for vec in parsed]


def make_ollama_embed_fn(
    ollama_url: str = DEFAULT_OLLAMA_URL,
    dims: int = DEFAULT_DIMS,
) -> EmbedFn:
    def _embed(texts: Sequence[str], model: str) -> list[list[float]]:
        return ollama_embed_batch(texts, model, ollama_url=ollama_url, dims=dims)

    return _embed


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


def schema_sql(dims: int = DEFAULT_DIMS) -> str:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    sql = re.sub(r"float\[\d+\]", f"float[{int(dims)}]", sql)
    sql = re.sub(
        r"dims INTEGER NOT NULL DEFAULT \d+",
        f"dims INTEGER NOT NULL DEFAULT {int(dims)}",
        sql,
    )
    return sql


def _ensure_meta_dims_column(conn: sqlite3.Connection, dims: int) -> None:
    if not _has_table(conn, "embedding_meta"):
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(embedding_meta)")}
    if "dims" not in cols:
        conn.execute(
            f"ALTER TABLE embedding_meta ADD COLUMN dims INTEGER NOT NULL DEFAULT {int(dims)}"
        )


def _assert_vec_dims(conn: sqlite3.Connection, dims: int) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'message_embeddings'"
    ).fetchone()
    if row is None or not row[0]:
        return
    if f"float[{int(dims)}]" not in row[0]:
        raise EmbedError(
            f"message_embeddings already exists with a different dimension than {dims}. "
            "This is expected if you previously applied the OpenAI 1536-d schema. "
            "Drop and recreate before backfill: "
            "DROP TABLE IF EXISTS message_embeddings; "
            "DROP TABLE IF EXISTS embedding_meta; "
            "(see scripts/README.md)."
        )


def apply_schema(conn: sqlite3.Connection, schema_sql_text: str | None = None, *, dims: int = DEFAULT_DIMS) -> None:
    sql = schema_sql_text if schema_sql_text is not None else schema_sql(dims)
    _assert_vec_dims(conn, dims)
    conn.executescript(sql)
    _ensure_meta_dims_column(conn, dims)
    _assert_vec_dims(conn, dims)
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
    stored = model_id(model)
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
            (stored, model_version, 1 if skip_auth else 0),
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
            (1 if skip_auth else 0, stored, model_version),
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
    stored = model_id(model)
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
    params: list = [stored, model_version, 1 if skip_auth else 0]
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
    dims: int = DEFAULT_DIMS,
) -> None:
    stored = model_id(model)
    if len(vector) != dims:
        raise EmbedError(
            f"Embedding dim {len(vector)} != {dims} "
            f"(v1 default is {DEFAULT_DIMS}-d Matryoshka from {NATIVE_DIMS} native; "
            f"stored model id {stored})."
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
          message_id, model, model_version, created_at, text_hash, char_count, dims
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id, model, model_version) DO UPDATE SET
          created_at = excluded.created_at,
          text_hash = excluded.text_hash,
          char_count = excluded.char_count,
          dims = excluded.dims
        """,
        (message_id, stored, model_version, created, text_hash, char_count, dims),
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
    ollama_url: str = DEFAULT_OLLAMA_URL,
    dims: int = DEFAULT_DIMS,
    embed_fn: EmbedFn | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Idempotent embed of FTS-backed messages. Resume-safe (commit per batch).

    Dry-run never calls Ollama (or any HTTP).
    """
    apply_schema(conn, dims=dims)
    stored = model_id(model)
    counts = candidate_counts(
        conn, model=model, model_version=model_version, skip_auth=skip_auth
    )
    emit = log or (lambda msg: print(msg, file=sys.stderr))
    emit(
        "backfill {c} candidate(s); skipped_auth={a} skipped_empty_body={e} "
        "skipped_already_embedded={s} reembed_hash_changed={h} joined={j} "
        "model={m} dims={d} ollama={u}".format(
            c=counts["candidates"],
            a=counts["skipped_auth"],
            e=counts["skipped_empty_body"],
            s=counts["skipped_already_embedded"],
            h=counts["reembed_hash_changed"],
            j=counts["joined"],
            m=stored,
            d=dims,
            u=ollama_url,
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

    worker = embed_fn or make_ollama_embed_fn(ollama_url, dims)

    embedded = 0
    size = max(1, int(batch_size))
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        texts = [row["text"] for row in batch]
        emit(
            f"embedding batch {start // size + 1} "
            f"({len(batch)} msgs, {start + 1}-{start + len(batch)} of {len(rows)}) "
            f"via {ollama_url} model={ollama_model_tag(model)}"
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
                dims=dims,
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
    dims: int = DEFAULT_DIMS,
) -> list[dict]:
    if len(query_vector) != dims:
        raise EmbedError(f"Query vector dim {len(query_vector)} != {dims}.")
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
    ollama_url: str = DEFAULT_OLLAMA_URL,
    dims: int = DEFAULT_DIMS,
    embed_fn: EmbedFn | None = None,
    extension_path: str | None = None,
    query_vector: Sequence[float] | None = None,
) -> list[dict]:
    """Embed `query` locally (or use `query_vector`) and return top-k cosine hits.

    FTS exact-id lookup stays on messages_fts — this does not replace it.
    """
    q = (query or "").strip()
    if not q and query_vector is None:
        raise EmbedError("Query text is empty.")
    conn = connect_db(db, extension_path)
    try:
        apply_schema(conn, dims=dims)
        vector: Sequence[float]
        if query_vector is not None:
            vector = query_vector
        else:
            worker = embed_fn or make_ollama_embed_fn(ollama_url, dims)
            vectors = worker([query_text(q)], model)
            if not vectors:
                raise EmbedError("Embedding the query returned no vector.")
            vector = vectors[0]
        return knn_search(conn, vector, k=k, dims=dims)
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
