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

from embed_document import (
    EMBED_MODEL_TAG,
    INSTRUCT_VERSION,
    document_embed_text,
)
from mail_clean import clean_body, content_hash
from thread_graph import thread_fields_from_row

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
# Modest ctx for §6.1 incremental docs (CHAR_CAP 16k ≈ 4k tokens). Live rem
# does not send num_ctx. 32k is the model max — do not use it on Mini.
DEFAULT_NUM_CTX = 4096
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

# PR-1 columns required by the §6.1 incremental path. Missing → refuse
# (do not ALTER here; run migrate_pr1_schema.py).
PR1_MESSAGE_COLS = (
    "thread_id",
    "in_reply_to",
    "references_header",
    "cleaned_body",
    "cleaned_chars",
    "content_hash",
)
PR1_META_COLS = (
    "embed_model",
    "embed_dim",
    "instruct_version",
    "quote_stripped",
    "content_hash",
)

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


def validate_shard(
    id_mod: int | None = None,
    id_rem: int | None = None,
) -> tuple[int | None, int | None]:
    """Require both shard flags together. ``id_mod >= 2`` and ``0 <= id_rem < id_mod``."""
    if id_mod is None and id_rem is None:
        return None, None
    if id_mod is None or id_rem is None:
        raise EmbedError(
            "both --id-mod N and --id-rem R are required together "
            "(omit both to embed all candidates)"
        )
    mod = int(id_mod)
    rem = int(id_rem)
    if mod < 2:
        raise EmbedError(f"--id-mod must be an integer >= 2, got {id_mod}")
    if rem < 0 or rem >= mod:
        raise EmbedError(f"--id-rem must satisfy 0 <= R < N (N={mod}), got {id_rem}")
    return mod, rem


def message_id_shard_hash(message_id: str) -> int:
    """Stable 64-bit int from SHA-1(utf-8 ``messages.id``), first 8 bytes big-endian.

    Text / UUID ids shard evenly. Do not parse the id as an integer.
    """
    digest = hashlib.sha1(str(message_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def shard_remainder(message_id: str, id_mod: int) -> int:
    return message_id_shard_hash(message_id) % int(id_mod)


def in_id_shard(
    message_id: str,
    id_mod: int | None,
    id_rem: int | None,
) -> bool:
    """True when flags are omitted, or ``hash(id) % id_mod == id_rem``."""
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    if id_mod is None:
        return True
    return shard_remainder(message_id, id_mod) == id_rem


def _shard_sql(
    conn: sqlite3.Connection,
    id_mod: int | None,
    id_rem: int | None,
    *,
    id_expr: str = "m.id",
) -> str:
    """Extra ``AND …`` clause, or ``""`` when not sharding. Registers a UDF."""
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    if id_mod is None:
        return ""
    mod, rem = id_mod, id_rem

    def _fn(value: object) -> int:
        return 1 if shard_remainder(str(value), mod) == rem else 0

    conn.create_function("mailroom_in_id_shard", 1, _fn)
    return f" AND mailroom_in_id_shard({id_expr})"


def validate_char_bounds(
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> tuple[int | None, int | None]:
    """Optional length window on ``len(embed_text(...))`` (after CHAR_CAP).

    Either flag may be omitted. On a **single** call with both set, the
    filters AND as a closed band: embed iff ``min_chars <= len <= max_chars``.
    ``max_chars >= min_chars`` is required (equal N keeps only that length).
    This is not an "overlap" error — ``--min-chars 1500 --max-chars 2000``
    is a valid band.

    Cross-machine partition still uses **one** flag per process at the same N
    (Mini ``--max-chars 1000``; MBP ``--min-chars 1000``).
    """
    if max_chars is None and min_chars is None:
        return None, None
    parsed_max = int(max_chars) if max_chars is not None else None
    parsed_min = int(min_chars) if min_chars is not None else None
    if parsed_max is not None and parsed_max < 0:
        raise EmbedError(f"--max-chars must be an integer >= 0, got {max_chars}")
    if parsed_min is not None and parsed_min < 0:
        raise EmbedError(f"--min-chars must be an integer >= 0, got {min_chars}")
    if (
        parsed_max is not None
        and parsed_min is not None
        and parsed_max < parsed_min
    ):
        raise EmbedError(
            "--max-chars must be >= --min-chars when both are set "
            f"(got max_chars={parsed_max} min_chars={parsed_min})"
        )
    return parsed_max, parsed_min


def char_bound_skip(
    n: int,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> str | None:
    """``'too_long'``, ``'too_short'``, or None if this payload should embed.

    ``--max-chars N`` keeps ``n <= N``. ``--min-chars N`` alone keeps ``n > N``
    (cross-machine split at the same N). Both together AND as a closed band:
    ``min_chars <= n <= max_chars``.
    """
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    if max_chars is not None and n > max_chars:
        return "too_long"
    if min_chars is not None:
        if max_chars is not None:
            if n < min_chars:
                return "too_short"
        elif n <= min_chars:
            return "too_short"
    return None


def in_char_bounds(
    n: int,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> bool:
    """True when flags are omitted, or the payload length passes both."""
    return char_bound_skip(n, max_chars, min_chars) is None


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
    num_ctx: int | None = None,
) -> list[list[float]]:
    """Embed via local Ollama. Prefers POST /api/embed; falls back to /v1/embeddings.

    No API key. Does not call api.openai.com. After `ollama pull`, this is offline.
    ``num_ctx`` is sent only when set (incremental §6.1 path). Live rem omits it.
    """
    if not texts:
        return []
    tag = ollama_model_tag(model)
    base = ollama_url.rstrip("/")
    native_payload: dict = {
        "model": tag,
        "input": list(texts),
        "keep_alive": "30m",
        "dimensions": int(dims),
    }
    if num_ctx is not None:
        native_payload["options"] = {"num_ctx": int(num_ctx)}
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
        compat_payload: dict = {
            "model": tag,
            "input": list(texts),
            "dimensions": int(dims),
        }
        if num_ctx is not None:
            compat_payload["options"] = {"num_ctx": int(num_ctx)}
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
    num_ctx: int | None = None,
) -> EmbedFn:
    def _embed(texts: Sequence[str], model: str) -> list[list[float]]:
        return ollama_embed_batch(
            texts, model, ollama_url=ollama_url, dims=dims, num_ctx=num_ctx
        )

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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}


def require_incremental_schema(conn: sqlite3.Connection) -> None:
    """Refuse rather than CREATE/DROP vec0. PR-1 columns must already exist."""
    if not _has_table(conn, "messages") or not _has_table(conn, "messages_fts"):
        raise EmbedError(
            "DB is missing messages / messages_fts. This script does not ingest mail."
        )
    if not _has_table(conn, "embedding_meta"):
        raise EmbedError(
            "embedding_meta missing — run PR-1 migrate; refuse (no vec0 create)"
        )
    if not _has_table(conn, "message_embeddings"):
        raise EmbedError(
            "message_embeddings missing — refuse (no vec0 create/rebuild)"
        )
    msg_cols = _table_columns(conn, "messages")
    meta_cols = _table_columns(conn, "embedding_meta")
    missing_msg = [c for c in PR1_MESSAGE_COLS if c not in msg_cols]
    missing_meta = [c for c in PR1_META_COLS if c not in meta_cols]
    if missing_msg or missing_meta:
        raise EmbedError(
            "PR-1 columns missing (run migrate_pr1_schema.py): "
            "messages missing %s; embedding_meta missing %s"
            % (missing_msg, missing_meta)
        )


def incremental_action(
    *,
    meta_present: bool,
    quote_stripped: int | None,
    stored_hash: str | None,
    new_hash: str,
) -> str:
    """``missing`` / ``stale`` / ``skip`` for one id.

    Live rem rows (meta present, ``content_hash`` NULL) are **skip** so this
    path never restarts the 54k/63k backfill.
    """
    if not meta_present:
        return "missing"
    stored = (stored_hash or "").strip()
    qs = 0
    try:
        qs = int(quote_stripped or 0)
    except (TypeError, ValueError):
        qs = 0
    if qs == 1 and stored and stored == new_hash:
        return "skip"
    if stored and stored != new_hash:
        return "stale"
    if stored and stored == new_hash and qs != 1:
        return "stale"
    return "skip"


def prepare_incremental_row(row: sqlite3.Row | dict, *, cap: int = CHAR_CAP) -> dict:
    """Clean body, thread fields, and header-prefixed document for one message."""
    mapping = dict(row) if not isinstance(row, dict) else row
    raw_body = mapping.get("body") or ""
    cleaned = clean_body(raw_body)
    digest = content_hash(cleaned)
    fields = thread_fields_from_row(mapping)
    from_addr = mapping.get("from_addr") or mapping.get("fts_from") or ""
    doc = document_embed_text(
        subject=mapping.get("subject"),
        from_addr=from_addr,
        from_name=mapping.get("from_name"),
        to_addrs=mapping.get("to_addrs"),
        date_iso=mapping.get("date_utc"),
        lane=mapping.get("lane"),
        cleaned_body=cleaned,
        cap=cap,
    )
    return {
        "id": mapping.get("id"),
        "source": mapping.get("source"),
        "lane": mapping.get("lane"),
        "subject": mapping.get("subject") or "",
        "body": raw_body,
        "from_addr": from_addr,
        "from_name": mapping.get("from_name") or "",
        "to_addrs": mapping.get("to_addrs") or "",
        "date_utc": mapping.get("date_utc") or "",
        "cleaned_body": cleaned,
        "cleaned_chars": len(cleaned),
        "content_hash": digest,
        "thread_id": fields.get("thread_id"),
        "in_reply_to": fields.get("in_reply_to"),
        "references_header": fields.get("references_header"),
        "message_id_header": mapping.get("message_id_header"),
        "text": doc,
        "text_hash": sha256_text(doc),
        "existing_content_hash": mapping.get("existing_content_hash"),
        "quote_stripped": mapping.get("quote_stripped"),
        "meta_present": mapping.get("meta_id") is not None,
    }


def write_message_incremental(conn: sqlite3.Connection, prepared: dict) -> None:
    """Persist thread + cleaned_body + content_hash. Raw FTS body is untouched."""
    conn.execute(
        """
        UPDATE messages SET
          thread_id = ?,
          in_reply_to = COALESCE(?, in_reply_to),
          references_header = COALESCE(?, references_header),
          cleaned_body = ?,
          cleaned_chars = ?,
          content_hash = ?
        WHERE id = ?
        """,
        (
            prepared.get("thread_id"),
            prepared.get("in_reply_to"),
            prepared.get("references_header"),
            prepared.get("cleaned_body"),
            prepared.get("cleaned_chars"),
            prepared.get("content_hash"),
            prepared["id"],
        ),
    )
    header = prepared.get("message_id_header")
    if header and _has_table(conn, "messages_ids"):
        conn.execute("DELETE FROM messages_ids WHERE id = ?", (prepared["id"],))
        conn.execute(
            "INSERT INTO messages_ids(id, message_id) VALUES (?, ?)",
            (prepared["id"], header),
        )


def _eligible_embed_rows(
    conn: sqlite3.Connection,
    *,
    model: str,
    model_version: str,
    skip_auth: bool,
    id_mod: int | None,
    id_rem: int | None,
) -> list:
    """Non-empty FTS body, optional skip-auth + shard. Includes already-embedded."""
    stored = model_id(model)
    shard = _shard_sql(conn, id_mod, id_rem)
    if _has_table(conn, "embedding_meta"):
        sql = f"""
            SELECT m.id AS id, m.source AS source, m.lane AS lane,
                   f.subject AS subject, f.body AS body, f.from_addr AS from_addr,
                   e.text_hash AS existing_hash
            FROM messages m
            JOIN messages_fts f ON f.id = m.id
            LEFT JOIN embedding_meta e
              ON e.message_id = m.id AND e.model = ? AND e.model_version = ?
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
            {shard}
            ORDER BY m.id
        """
        params: list = [stored, model_version, 1 if skip_auth else 0]
    else:
        sql = f"""
            SELECT m.id AS id, m.source AS source, m.lane AS lane,
                   f.subject AS subject, f.body AS body, f.from_addr AS from_addr,
                   NULL AS existing_hash
            FROM messages m
            JOIN messages_fts f ON f.id = m.id
            WHERE TRIM(COALESCE(f.body, '')) != ''
              AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth')
            {shard}
            ORDER BY m.id
        """
        params = [1 if skip_auth else 0]
    return list(conn.execute(sql, params).fetchall())


def candidate_counts(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    id_mod: int | None = None,
    id_rem: int | None = None,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> dict[str, int]:
    """Dry-run stats: auth / empty-body / already-embedded / hash-changed / due.

    When ``id_mod`` / ``id_rem`` are set, every count is restricted to that
    shard (``hash(messages.id) % id_mod == id_rem``). Omit both for all rows.
    ``max_chars`` / ``min_chars`` filter on ``len(embed_text(...))`` after
    CHAR_CAP and increment ``skipped_too_long`` / ``skipped_too_short``.
    """
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    if not _has_table(conn, "messages") or not _has_table(conn, "messages_fts"):
        raise EmbedError(
            "DB is missing messages / messages_fts. This script does not ingest mail."
        )
    shard = _shard_sql(conn, id_mod, id_rem)
    auth_clause = "LOWER(COALESCE(m.lane, '')) = 'auth'"
    empty_clause = "TRIM(COALESCE(f.body, '')) = ''"
    joined_sql = "SELECT COUNT(*) FROM messages m JOIN messages_fts f ON f.id = m.id"
    if shard:
        joined_sql += " WHERE 1=1" + shard
    total = conn.execute(joined_sql).fetchone()[0]
    auth = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {auth_clause}
        {shard}
        """
    ).fetchone()[0]
    empty = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {empty_clause}
          AND (? = 0 OR NOT ({auth_clause}))
        {shard}
        """,
        (1 if skip_auth else 0,),
    ).fetchone()[0]
    already = 0
    hash_changed = 0
    due = 0
    skipped_too_long = 0
    skipped_too_short = 0
    for row in _eligible_embed_rows(
        conn,
        model=model,
        model_version=model_version,
        skip_auth=skip_auth,
        id_mod=id_mod,
        id_rem=id_rem,
    ):
        payload = embed_text(row["subject"], row["body"])
        existing = row["existing_hash"]
        if existing and existing == sha256_text(payload):
            already += 1
            continue
        reason = char_bound_skip(len(payload), max_chars, min_chars)
        if reason == "too_long":
            skipped_too_long += 1
            continue
        if reason == "too_short":
            skipped_too_short += 1
            continue
        if existing:
            hash_changed += 1
        else:
            due += 1
    out = {
        "joined": int(total),
        "skipped_auth": int(auth) if skip_auth else 0,
        "skipped_empty_body": int(empty),
        "skipped_already_embedded": int(already),
        "skipped_too_long": int(skipped_too_long),
        "skipped_too_short": int(skipped_too_short),
        "reembed_hash_changed": int(hash_changed),
        "candidates": int(due) + int(hash_changed),
    }
    if id_mod is not None:
        out["id_mod"] = int(id_mod)
        out["id_rem"] = int(id_rem)
    return out


def iter_candidates(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    limit: int | None = None,
    id_mod: int | None = None,
    id_rem: int | None = None,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> list[dict]:
    """Messages with a non-empty FTS body, not auth, needing (re)embed.

    Optional ``id_mod`` / ``id_rem`` keep only ids whose stable hash lands in
    that shard. Optional ``max_chars`` / ``min_chars`` keep only payloads
    whose ``len(embed_text(...))`` (CHAR_CAP first) is ``<= max`` / ``> min``.
    Both together AND as a closed band (``min <= len <= max``). ``limit`` applies after
    shard and char filters.
    """
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    if not _has_table(conn, "messages") or not _has_table(conn, "messages_fts"):
        raise EmbedError(
            "DB is missing messages / messages_fts. This script does not ingest mail."
        )
    rows = _eligible_embed_rows(
        conn,
        model=model,
        model_version=model_version,
        skip_auth=skip_auth,
        id_mod=id_mod,
        id_rem=id_rem,
    )
    out = []
    for row in rows:
        if not in_id_shard(row["id"], id_mod, id_rem):
            continue
        payload = embed_text(row["subject"], row["body"])
        digest = sha256_text(payload)
        existing = row["existing_hash"]
        if existing and existing == digest:
            continue
        if char_bound_skip(len(payload), max_chars, min_chars):
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


def _eligible_incremental_rows(
    conn: sqlite3.Connection,
    *,
    model: str,
    model_version: str,
    skip_auth: bool,
    id_mod: int | None,
    id_rem: int | None,
) -> list:
    """FTS-backed rows plus PR-1 / live header columns. Includes rem-owned ids."""
    stored = model_id(model)
    shard = _shard_sql(conn, id_mod, id_rem)
    msg_cols = _table_columns(conn, "messages")
    meta_cols = _table_columns(conn, "embedding_meta") if _has_table(conn, "embedding_meta") else set()

    def mcol(name: str, alias: str | None = None) -> str:
        dest = alias or name
        if name in msg_cols:
            return "m.%s AS %s" % (name, dest)
        return "NULL AS %s" % dest

    select = [
        "m.id AS id",
        mcol("source"),
        mcol("lane"),
        mcol("from_addr"),
        mcol("from_name"),
        mcol("to_addrs"),
        mcol("date_utc"),
        mcol("message_id_header"),
        mcol("in_reply_to"),
        mcol("references_header"),
        "f.subject AS subject",
        "f.body AS body",
        "f.from_addr AS fts_from",
    ]
    params: list = []
    join = ""
    if _has_table(conn, "embedding_meta"):
        select.append("e.message_id AS meta_id")
        select.append("e.text_hash AS existing_hash")
        if "content_hash" in meta_cols:
            select.append("e.content_hash AS existing_content_hash")
        else:
            select.append("NULL AS existing_content_hash")
        if "quote_stripped" in meta_cols:
            select.append("e.quote_stripped AS quote_stripped")
        else:
            select.append("NULL AS quote_stripped")
        join = (
            "LEFT JOIN embedding_meta e "
            "ON e.message_id = m.id AND e.model = ? AND e.model_version = ?"
        )
        params.extend([stored, model_version])
    else:
        select.append("NULL AS meta_id")
        select.append("NULL AS existing_hash")
        select.append("NULL AS existing_content_hash")
        select.append("NULL AS quote_stripped")
    params.append(1 if skip_auth else 0)
    sql = (
        "SELECT %s FROM messages m "
        "JOIN messages_fts f ON f.id = m.id %s "
        "WHERE TRIM(COALESCE(f.body, '')) != '' "
        "AND (? = 0 OR LOWER(COALESCE(m.lane, '')) != 'auth') "
        "%s ORDER BY m.id"
    ) % (", ".join(select), join, shard)
    return list(conn.execute(sql, params).fetchall())


def iter_incremental_candidates(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    limit: int | None = None,
    id_mod: int | None = None,
    id_rem: int | None = None,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> list[dict]:
    """§6.1 candidates: missing from embedding_meta or stale content_hash.

    Skips ``quote_stripped=1`` with matching ``content_hash``. Skips live rem
    rows that have meta but no ``embedding_meta.content_hash`` (no 63k restart).
    Char bounds apply to the header-prefixed document (CHAR_CAP first).
    """
    require_incremental_schema(conn)
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    rows = _eligible_incremental_rows(
        conn,
        model=model,
        model_version=model_version,
        skip_auth=skip_auth,
        id_mod=id_mod,
        id_rem=id_rem,
    )
    out: list[dict] = []
    for row in rows:
        if not in_id_shard(row["id"], id_mod, id_rem):
            continue
        prepared = prepare_incremental_row(row)
        action = incremental_action(
            meta_present=bool(prepared["meta_present"]),
            quote_stripped=prepared.get("quote_stripped"),
            stored_hash=prepared.get("existing_content_hash"),
            new_hash=prepared["content_hash"],
        )
        if action == "skip":
            continue
        if char_bound_skip(len(prepared["text"]), max_chars, min_chars):
            continue
        prepared["reembed"] = action == "stale"
        prepared["action"] = action
        out.append(prepared)
        if limit is not None and len(out) >= limit:
            break
    return out


def incremental_candidate_counts(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    skip_auth: bool = True,
    id_mod: int | None = None,
    id_rem: int | None = None,
    max_chars: int | None = None,
    min_chars: int | None = None,
) -> dict[str, int]:
    """Dry-run stats for the §6.1 path. Does not walk the 54k rem-owned rows as due."""
    require_incremental_schema(conn)
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    shard = _shard_sql(conn, id_mod, id_rem)
    auth_clause = "LOWER(COALESCE(m.lane, '')) = 'auth'"
    empty_clause = "TRIM(COALESCE(f.body, '')) = ''"
    joined_sql = "SELECT COUNT(*) FROM messages m JOIN messages_fts f ON f.id = m.id"
    if shard:
        joined_sql += " WHERE 1=1" + shard
    total = conn.execute(joined_sql).fetchone()[0]
    auth = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {auth_clause}
        {shard}
        """
    ).fetchone()[0]
    empty = conn.execute(
        f"""
        SELECT COUNT(*) FROM messages m
        JOIN messages_fts f ON f.id = m.id
        WHERE {empty_clause}
          AND (? = 0 OR NOT ({auth_clause}))
        {shard}
        """,
        (1 if skip_auth else 0,),
    ).fetchone()[0]
    skipped_quote_stripped = 0
    skipped_legacy_embedded = 0
    skipped_too_long = 0
    skipped_too_short = 0
    stale = 0
    due = 0
    for row in _eligible_incremental_rows(
        conn,
        model=model,
        model_version=model_version,
        skip_auth=skip_auth,
        id_mod=id_mod,
        id_rem=id_rem,
    ):
        prepared = prepare_incremental_row(row)
        action = incremental_action(
            meta_present=bool(prepared["meta_present"]),
            quote_stripped=prepared.get("quote_stripped"),
            stored_hash=prepared.get("existing_content_hash"),
            new_hash=prepared["content_hash"],
        )
        if action == "skip":
            qs = 0
            try:
                qs = int(prepared.get("quote_stripped") or 0)
            except (TypeError, ValueError):
                qs = 0
            if qs == 1:
                skipped_quote_stripped += 1
            elif prepared["meta_present"]:
                skipped_legacy_embedded += 1
            continue
        reason = char_bound_skip(len(prepared["text"]), max_chars, min_chars)
        if reason == "too_long":
            skipped_too_long += 1
            continue
        if reason == "too_short":
            skipped_too_short += 1
            continue
        if action == "stale":
            stale += 1
        else:
            due += 1
    out = {
        "joined": int(total),
        "skipped_auth": int(auth) if skip_auth else 0,
        "skipped_empty_body": int(empty),
        "skipped_already_embedded": int(skipped_quote_stripped),
        "skipped_quote_stripped": int(skipped_quote_stripped),
        "skipped_legacy_embedded": int(skipped_legacy_embedded),
        "skipped_too_long": int(skipped_too_long),
        "skipped_too_short": int(skipped_too_short),
        "reembed_hash_changed": int(stale),
        "reembed_stale_content_hash": int(stale),
        "candidates": int(due) + int(stale),
    }
    if id_mod is not None:
        out["id_mod"] = int(id_mod)
        out["id_rem"] = int(id_rem)
    return out


def _write_quote_strip_meta(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    stored_model: str,
    model_version: str,
    content_hash_value: str,
    embed_model: str,
    embed_dim: int,
    instruct_version: str,
) -> None:
    cols = _table_columns(conn, "embedding_meta")
    assignments: list[str] = []
    params: list = []
    if "embed_model" in cols:
        assignments.append("embed_model = ?")
        params.append(embed_model)
    if "embed_dim" in cols:
        assignments.append("embed_dim = ?")
        params.append(int(embed_dim))
    if "instruct_version" in cols:
        assignments.append("instruct_version = ?")
        params.append(instruct_version)
    if "quote_stripped" in cols:
        assignments.append("quote_stripped = 1")
    if "content_hash" in cols:
        assignments.append("content_hash = ?")
        params.append(content_hash_value)
    if "source" in cols:
        assignments.append("source = 'message'")
    if not assignments:
        return
    params.extend([message_id, stored_model, model_version])
    conn.execute(
        "UPDATE embedding_meta SET %s "
        "WHERE message_id = ? AND model = ? AND model_version = ?"
        % ", ".join(assignments),
        params,
    )


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
    quote_stripped: bool = False,
    content_hash_value: str | None = None,
    embed_model: str | None = None,
    instruct_version: str | None = None,
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
    if quote_stripped:
        _write_quote_strip_meta(
            conn,
            message_id=message_id,
            stored_model=stored,
            model_version=model_version,
            content_hash_value=content_hash_value or "",
            embed_model=embed_model or EMBED_MODEL_TAG,
            embed_dim=dims,
            instruct_version=instruct_version or INSTRUCT_VERSION,
        )


def _run_locked_batch(
    use_lock: bool,
    purpose: str,
    fn: Callable[[], None],
    *,
    lock_path: Path | None = None,
    action_required_path: Path | None = None,
) -> None:
    """Hold the PR-0 writer lock for one batch heartbeat. Not the whole rem."""
    if not use_lock:
        fn()
        return
    try:
        import with_writer_lock as wwl
    except ImportError as exc:
        raise EmbedError("with_writer_lock missing; omit --lock") from exc
    action = action_required_path or wwl.default_action_required_path()
    if wwl.action_required_open(action):
        raise EmbedError("action-required open ⇒ no lock / no writes (%s)" % action)
    held = wwl.acquire_writer_lock(lock_path or wwl.default_lock_path(), purpose)
    try:
        fn()
    finally:
        wwl.release_writer_lock(held)


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
    id_mod: int | None = None,
    id_rem: int | None = None,
    max_chars: int | None = None,
    min_chars: int | None = None,
    quote_strip: bool = False,
    num_ctx: int | None = None,
    lock: bool = False,
    lock_path: Path | None = None,
    action_required_path: Path | None = None,
    lock_purpose: str = "embed_batch",
) -> dict[str, int]:
    """Idempotent embed of FTS-backed messages. Resume-safe (commit per batch).

    Dry-run never calls Ollama (or any HTTP). Optional ``id_mod`` / ``id_rem``
    restrict work to one shard of ``messages.id``. Optional ``max_chars`` /
    ``min_chars`` restrict work by payload length after CHAR_CAP.

    Default path (``quote_strip=False``) is the live rem text
    (``subject + body``). ``quote_strip=True`` is MAILROOM §6.1 incremental:
    header-prefixed cleaned body, no vec0 create/drop, no 63k rem restart.
    Writer lock is per batch/heartbeat when ``lock`` is set — not the whole rem.
    """
    id_mod, id_rem = validate_shard(id_mod, id_rem)
    max_chars, min_chars = validate_char_bounds(max_chars, min_chars)
    if quote_strip:
        require_incremental_schema(conn)
        if dims != DEFAULT_DIMS:
            raise EmbedError(
                "incremental path is %s-d (got dims=%s); refuse (no vec0 rebuild)"
                % (DEFAULT_DIMS, dims)
            )
        counts = incremental_candidate_counts(
            conn,
            model=model,
            model_version=model_version,
            skip_auth=skip_auth,
            id_mod=id_mod,
            id_rem=id_rem,
            max_chars=max_chars,
            min_chars=min_chars,
        )
    else:
        apply_schema(conn, dims=dims)
        counts = candidate_counts(
            conn,
            model=model,
            model_version=model_version,
            skip_auth=skip_auth,
            id_mod=id_mod,
            id_rem=id_rem,
            max_chars=max_chars,
            min_chars=min_chars,
        )
    stored = model_id(model)
    emit = log or (lambda msg: print(msg, file=sys.stderr))
    path_note = " quote_strip=1 instruct=%s" % INSTRUCT_VERSION if quote_strip else ""
    emit(
        "backfill {c} candidate(s); skipped_auth={a} skipped_empty_body={e} "
        "skipped_already_embedded={s} skipped_too_long={tl} "
        "skipped_too_short={ts} reembed_hash_changed={h} joined={j} "
        "model={m} dims={d} ollama={u}{p}".format(
            c=counts["candidates"],
            a=counts["skipped_auth"],
            e=counts["skipped_empty_body"],
            s=counts["skipped_already_embedded"],
            tl=counts["skipped_too_long"],
            ts=counts["skipped_too_short"],
            h=counts["reembed_hash_changed"],
            j=counts["joined"],
            m=stored,
            d=dims,
            u=ollama_url,
            p=path_note,
        )
    )
    if quote_strip:
        emit(
            "incremental §6.1: skipped_legacy_embedded=%s skipped_quote_stripped=%s "
            "stale_content_hash=%s (live rem rows without content_hash are skipped)"
            % (
                counts.get("skipped_legacy_embedded", 0),
                counts.get("skipped_quote_stripped", 0),
                counts.get("reembed_stale_content_hash", 0),
            )
        )
    if id_mod is not None:
        emit(
            f"shard {id_rem}/{id_mod}: candidates in shard={counts['candidates']} "
            f"joined_in_shard={counts['joined']} "
            f"(stable SHA-1 of messages.id, first 8 bytes, % {id_mod} == {id_rem})"
        )
    if max_chars is not None or min_chars is not None:
        emit(
            f"char filter: max_chars={max_chars} min_chars={min_chars} "
            f"skipped_too_long={counts['skipped_too_long']} "
            f"skipped_too_short={counts['skipped_too_short']}"
        )
    if quote_strip:
        rows = iter_incremental_candidates(
            conn,
            model=model,
            model_version=model_version,
            skip_auth=skip_auth,
            limit=limit,
            id_mod=id_mod,
            id_rem=id_rem,
            max_chars=max_chars,
            min_chars=min_chars,
        )
    else:
        rows = iter_candidates(
            conn,
            model=model,
            model_version=model_version,
            skip_auth=skip_auth,
            limit=limit,
            id_mod=id_mod,
            id_rem=id_rem,
            max_chars=max_chars,
            min_chars=min_chars,
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

    ctx = num_ctx if quote_strip else None
    if quote_strip and ctx is None:
        ctx = DEFAULT_NUM_CTX
    worker = embed_fn or make_ollama_embed_fn(ollama_url, dims, num_ctx=ctx)

    embedded = 0
    size = max(1, int(batch_size))
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        texts = [row["text"] for row in batch]
        emit(
            f"embedding batch {start // size + 1} "
            f"({len(batch)} msgs, {start + 1}-{start + len(batch)} of {len(rows)}) "
            f"via {ollama_url} model={ollama_model_tag(model)}"
            + (" num_ctx=%s" % ctx if ctx is not None else "")
        )
        vectors = worker(texts, model)
        if len(vectors) != len(batch):
            raise EmbedError("embed function returned the wrong number of vectors")

        def _commit_batch(
            batch_rows: list[dict] = batch,
            batch_vectors: list[list[float]] = vectors,
        ) -> None:
            nonlocal embedded
            for row, vector in zip(batch_rows, batch_vectors):
                if quote_strip:
                    write_message_incremental(conn, row)
                upsert_embedding(
                    conn,
                    message_id=row["id"],
                    vector=vector,
                    model=model,
                    model_version=model_version,
                    text_hash=row["text_hash"],
                    char_count=len(row["text"]),
                    dims=dims,
                    quote_stripped=quote_strip,
                    content_hash_value=row.get("content_hash"),
                    embed_model=EMBED_MODEL_TAG,
                    instruct_version=INSTRUCT_VERSION,
                )
                embedded += 1
            conn.commit()

        _run_locked_batch(
            lock,
            lock_purpose,
            _commit_batch,
            lock_path=lock_path,
            action_required_path=action_required_path,
        )
        emit(f"committed {embedded}/{len(rows)}")
    counts["embedded"] = embedded
    counts["would_embed"] = 0
    return counts


def vec_declared_dims(conn: sqlite3.Connection) -> int | None:
    """Declared ``float[N]`` on ``message_embeddings``, or None if missing."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'message_embeddings'"
    ).fetchone()
    if row is None or not row[0]:
        return None
    match = re.search(r"float\[(\d+)\]", row[0])
    return int(match.group(1)) if match else None


def _embedding_blob(conn: sqlite3.Connection, message_id: str) -> bytes | None:
    row = conn.execute(
        "SELECT embedding FROM message_embeddings WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    blob = row[0] if not isinstance(row, sqlite3.Row) else row["embedding"]
    if blob is None:
        return None
    return bytes(blob)


def merge_shards(
    primary: sqlite3.Connection,
    secondary: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    model_version: str = DEFAULT_MODEL_VERSION,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Copy missing embed rows from ``secondary`` into ``primary``.

    Missing-only: a primary ``embedding_meta`` row for the same
    ``(message_id, model, model_version)`` is left untouched, even if
    ``text_hash`` differs. Never deletes primary rows. Never writes
    ``messages`` / FTS. Never calls Ollama or IMAP.

    Idempotent: a second run reports ``skipped_already_present`` for every
    previously copied id.
    """
    emit = log or (lambda msg: print(msg, file=sys.stderr))
    stored = model_id(model)
    if not _has_table(secondary, "embedding_meta") or not _has_table(
        secondary, "message_embeddings"
    ):
        raise EmbedError(
            "secondary DB is missing embedding_meta / message_embeddings. "
            "Run embed_backfill.py on the copy first."
        )
    sec_dims = vec_declared_dims(secondary)
    pri_dims = vec_declared_dims(primary)
    if sec_dims is None:
        raise EmbedError("secondary message_embeddings has no declared float[N] dims")
    if pri_dims is None:
        apply_schema(primary, dims=sec_dims)
        pri_dims = vec_declared_dims(primary)
    if pri_dims != sec_dims:
        raise EmbedError(
            f"dims mismatch: primary message_embeddings is float[{pri_dims}], "
            f"secondary is float[{sec_dims}]. Same model / CHAR_CAP / --dims "
            "required on both machines; do not change mid-corpus."
        )
    _ensure_meta_dims_column(primary, pri_dims)
    _ensure_meta_dims_column(secondary, sec_dims)

    dim_rows = secondary.execute(
        """
        SELECT DISTINCT dims FROM embedding_meta
        WHERE model = ? AND model_version = ?
        """,
        (stored, model_version),
    ).fetchall()
    sec_meta_dims = {int(row[0]) for row in dim_rows if row[0] is not None}
    if sec_meta_dims and sec_meta_dims != {int(sec_dims)}:
        raise EmbedError(
            f"secondary embedding_meta.dims {sorted(sec_meta_dims)} "
            f"!= table float[{sec_dims}] for model={stored} version={model_version}"
        )

    rows = secondary.execute(
        """
        SELECT message_id, model, model_version, created_at, text_hash, char_count, dims
        FROM embedding_meta
        WHERE model = ? AND model_version = ?
        ORDER BY message_id
        """,
        (stored, model_version),
    ).fetchall()

    examined = 0
    inserted = 0
    skipped_already_present = 0
    missing_vector = 0
    errors = 0

    emit(
        f"merge secondary → primary model={stored} version={model_version} "
        f"dims={sec_dims} examined_source_rows={len(rows)}"
        + (" dry-run" if dry_run else "")
    )

    try:
        for row in rows:
            examined += 1
            message_id = row["message_id"]
            try:
                exists = primary.execute(
                    """
                    SELECT 1 FROM embedding_meta
                    WHERE message_id = ? AND model = ? AND model_version = ?
                    """,
                    (message_id, stored, model_version),
                ).fetchone()
                if exists:
                    skipped_already_present += 1
                    continue
                blob = _embedding_blob(secondary, message_id)
                if blob is None:
                    missing_vector += 1
                    emit(f"missing_vector on secondary: {message_id}")
                    continue
                expected_bytes = int(sec_dims) * 4
                if len(blob) != expected_bytes:
                    errors += 1
                    emit(
                        f"error {message_id}: vector is {len(blob)} bytes, "
                        f"expected {expected_bytes} for dims={sec_dims}"
                    )
                    continue
                if dry_run:
                    inserted += 1
                    continue
                has_vec = primary.execute(
                    "SELECT 1 FROM message_embeddings WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if has_vec is None:
                    primary.execute(
                        "INSERT INTO message_embeddings(message_id, embedding) "
                        "VALUES (?, ?)",
                        (message_id, blob),
                    )
                primary.execute(
                    """
                    INSERT INTO embedding_meta(
                      message_id, model, model_version, created_at,
                      text_hash, char_count, dims
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id, model, model_version) DO NOTHING
                    """,
                    (
                        message_id,
                        stored,
                        model_version,
                        row["created_at"],
                        row["text_hash"],
                        row["char_count"],
                        int(row["dims"] if row["dims"] is not None else sec_dims),
                    ),
                )
                inserted += 1
            except (sqlite3.Error, EmbedError, TypeError, ValueError) as exc:
                errors += 1
                emit(f"error {message_id}: {exc}")
        if not dry_run:
            primary.commit()
    except Exception:
        if not dry_run:
            primary.rollback()
        raise

    counts = {
        "examined": examined,
        "inserted": inserted,
        "skipped_already_present": skipped_already_present,
        "missing_vector": missing_vector,
        "errors": errors,
    }
    emit(
        "merge {verb}: examined={examined} inserted={inserted} "
        "skipped_already_present={skipped_already_present} "
        "missing_vector={missing_vector} errors={errors}".format(
            verb="dry-run would insert" if dry_run else "committed",
            **counts,
        )
    )
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
