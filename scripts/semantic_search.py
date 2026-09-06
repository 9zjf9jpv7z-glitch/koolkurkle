#!/usr/bin/env python3
"""MAILROOM.md §6.2 hybrid retrieve (Heavy PR-6).

Locked API::

    retrieve(query, k=20, lane=None, after=None, before=None) -> list[Hit]

    Hit = {message_id, chunk_id, thread_id, date, from, subject, snippet,
           fts_rank, vec_rank, rrf, rerank, lane}

Pipeline: infer lane + optional date window → FTS5 BM25 top-50 (subject-boost)
→ query embed instruct_version=v1 / dims=1024 → sqlite-vec KNN top-50 via
live ``message_embeddings`` (vec0) LEFT JOIN ``chunk_vec_map`` → RRF
``1/(60+rank)`` (missing=1000) → recency ``exp(-0.002*age_days)`` unless a
date window was set → identifier-shaped queries also MATCH ``messages_ids``
→ rerank stub (None, fail-open) → thread spine expand → dedup by thread_id
for the generator pack.

Lane + date filters (documented):
  * ``lane=None`` infers money (money words) / people (name-shaped) / none.
    Explicit ``lane`` wins. ``lane="none"`` or ``""`` skips inference and
    does not filter.
  * FTS: **pre-filter** on ``messages.lane`` / ``messages.date_utc``.
  * Vec: **post-filter** after sqlite-vec KNN (k applies in embedding space).
  * Recency decay is skipped when ``after`` or ``before`` is set.

CLI (Mac smoke; Mini venv — Apple /usr/bin/python3 cannot load sqlite-vec)::

    ~/MailArchive/.venv/bin/python scripts/semantic_search.py 'SDGE bill'
    ~/MailArchive/.venv/bin/python scripts/semantic_search.py --json --k 20 'Caddell'
    ~/MailArchive/.venv/bin/python scripts/semantic_search.py --lane money --after 2024-01-01 'invoice'
    ~/MailArchive/.venv/bin/python scripts/semantic_search.py --cosine 'SDGE bill'

``--cosine`` is the backward-compatible embed_lib.semantic_search path.
Default invocation is hybrid ``retrieve()``.

Query-side v1 prefix only — does not re-embed the 63k document rows.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402
import messages_ids as mids  # noqa: E402
from embed_document import INSTRUCT_VERSION  # noqa: E402

DEFAULT_DB = el.DEFAULT_DB
DEFAULT_K = 20
COSINE_K = el.DEFAULT_K
FTS_K = 50
VEC_K = 50
IDS_K = 50
RRF_K = 60
MISSING_RANK = 1000
RECENCY_LAMBDA = 0.002
SUBJECT_BOOST = 5.0
QUERY_DIMS = 1024
QUERY_INSTRUCT_VERSION = INSTRUCT_VERSION  # v1 — query prefix only

HIT_FIELDS = (
    "message_id",
    "chunk_id",
    "thread_id",
    "date",
    "from",
    "subject",
    "snippet",
    "fts_rank",
    "vec_rank",
    "rrf",
    "rerank",
    "lane",
)

MONEY_WORDS = frozenset(
    {
        "invoice",
        "invoices",
        "bill",
        "bills",
        "billing",
        "payment",
        "payments",
        "paid",
        "pay",
        "receipt",
        "receipts",
        "refund",
        "refunds",
        "amount",
        "due",
        "usd",
        "dollar",
        "dollars",
        "charge",
        "charges",
        "statement",
        "statements",
        "tax",
        "taxes",
        "payroll",
        "wire",
        "ach",
        "balance",
        "fee",
        "fees",
        "debit",
        "credit",
        "subscription",
        "purchase",
        "checkout",
        "overdue",
        "vendor",
        "sdge",
        "utility",
        "utilities",
    }
)

_PEOPLE_STOP = frozenset(
    {
        "the",
        "this",
        "that",
        "from",
        "your",
        "what",
        "when",
        "where",
        "who",
        "how",
        "about",
        "please",
        "email",
        "mail",
        "message",
        "subject",
        "inbox",
        "today",
        "yesterday",
        "week",
        "month",
        "year",
        "hello",
        "thanks",
        "thank",
        "regards",
    }
)
_TITLE_NAME = re.compile(r"\b([A-Z][a-z]{2,})\b")
_TWO_NAMES = re.compile(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b")
_FROM_NAME = re.compile(
    r"\b(?:from|to|fwd|forward(?:ed)?(?:\s+to)?)\s+([A-Z][a-z]{2,})\b"
)

VecHitsFn = Callable[..., list[dict]]
EmbedFn = el.EmbedFn


class RetrieveError(RuntimeError):
    """Hybrid retrieve failure (never includes secrets)."""


def default_db_path() -> Path:
    raw = os.environ.get("MAILROOM_DB")
    return Path(raw).expanduser() if raw else Path(DEFAULT_DB)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
            return datetime.fromisoformat(str(value).strip() + "T00:00:00+00:00")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def infer_lane(query: str, explicit: str | None = None) -> str | None:
    """money words → money; people names → people; else None.

    Explicit ``lane`` wins. ``none`` / ``""`` disables inference (no filter).
    """
    if explicit is not None:
        raw = str(explicit).strip().lower()
        if raw in ("", "none", "null", "*"):
            return None
        return raw
    q = (query or "").strip()
    if not q:
        return None
    tokens = re.findall(r"[A-Za-z$]+", q.lower())
    if any(tok.lstrip("$") in MONEY_WORDS for tok in tokens) or "$" in q:
        return "money"
    if _TWO_NAMES.search(q) or _FROM_NAME.search(q):
        return "people"
    titles = [
        m.group(1)
        for m in _TITLE_NAME.finditer(q)
        if m.group(1).lower() not in _PEOPLE_STOP
    ]
    if titles:
        return "people"
    return None


def query_embed_text(query: str) -> str:
    """v1 search payload: Qwen3 instruct prefix (documents are not re-prefixed)."""
    return el.query_text(query)


def rrf_score(
    fts_rank: int | None,
    vec_rank: int | None,
    *,
    ids_rank: int | None = None,
    include_ids: bool = False,
    k: int = RRF_K,
    missing: int = MISSING_RANK,
) -> float:
    fts = missing if fts_rank is None else int(fts_rank)
    vec = missing if vec_rank is None else int(vec_rank)
    score = 1.0 / (k + fts) + 1.0 / (k + vec)
    if include_ids:
        ids = missing if ids_rank is None else int(ids_rank)
        score += 1.0 / (k + ids)
    return score


def recency_multiplier(
    date_value: str | None,
    *,
    now: datetime | None = None,
    decay: float = RECENCY_LAMBDA,
) -> float:
    dt = parse_iso_datetime(date_value)
    if dt is None:
        return 1.0
    current = now or utc_now()
    age_days = (current - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return math.exp(-decay * age_days)


def _quote_fts_token(token: str) -> str:
    cleaned = token.replace('"', " ").strip()
    if not cleaned:
        return ""
    return '"%s"' % cleaned


def fts_match_query(query: str) -> str:
    """AND of quoted tokens so odd punctuation does not break FTS5 MATCH."""
    tokens = re.findall(r"[A-Za-z0-9_]+(?:[-.][A-Za-z0-9_]+)*", query or "")
    quoted = [_quote_fts_token(t) for t in tokens]
    quoted = [q for q in quoted if q]
    if not quoted:
        fallback = _quote_fts_token(query or "")
        return fallback or '""'
    return " AND ".join(quoted)


def _date_bounds(after: str | None, before: str | None) -> tuple[str | None, str | None]:
    """Inclusive ISO lower / exclusive-or-inclusive upper for date_utc string compare.

    Date-only ``before`` includes that calendar day (upper = before + T23:59:59).
    """
    lo = None
    hi = None
    if after:
        after_s = str(after).strip()
        lo = after_s if after_s else None
    if before:
        before_s = str(before).strip()
        if before_s and re.fullmatch(r"\d{4}-\d{2}-\d{2}", before_s):
            hi = before_s + "T23:59:59.999999Z"
        else:
            hi = before_s or None
    return lo, hi


def _message_filters_sql(
    conn: sqlite3.Connection,
    *,
    alias: str,
    lane: str | None,
    after: str | None,
    before: str | None,
) -> tuple[str, list[Any]]:
    cols = set(table_columns(conn, "messages"))
    clauses: list[str] = []
    params: list[Any] = []
    if lane and "lane" in cols:
        clauses.append("LOWER(COALESCE(%s.lane, '')) = ?" % alias)
        params.append(lane.lower())
    date_col = "date_utc" if "date_utc" in cols else ("date" if "date" in cols else None)
    lo, hi = _date_bounds(after, before)
    if date_col and lo:
        clauses.append("COALESCE(%s.%s, '') >= ?" % (alias, date_col))
        params.append(lo)
    if date_col and hi:
        clauses.append("COALESCE(%s.%s, '') <= ?" % (alias, date_col))
        params.append(hi)
    if not clauses:
        return "", params
    return " AND " + " AND ".join(clauses), params


def _bm25_weight_sql(fts_cols: list[str], *, subject_boost: float = SUBJECT_BOOST) -> str:
    weights: list[str] = []
    for col in fts_cols:
        if col == "id":
            weights.append("0.0")
        elif col == "subject":
            weights.append(str(float(subject_boost)))
        else:
            weights.append("1.0")
    if not weights:
        return "bm25(messages_fts)"
    return "bm25(messages_fts, %s)" % ", ".join(weights)


def _empty_hit(message_id: str, **overrides: Any) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "message_id": message_id,
        "chunk_id": None,
        "thread_id": None,
        "date": None,
        "from": None,
        "subject": None,
        "snippet": None,
        "fts_rank": MISSING_RANK,
        "vec_rank": MISSING_RANK,
        "rrf": 0.0,
        "rerank": None,
        "lane": None,
    }
    hit.update(overrides)
    return hit


def _load_message(conn: sqlite3.Connection, message_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "message_id": message_id,
        "chunk_id": None,
        "thread_id": None,
        "date": None,
        "from": None,
        "subject": None,
        "snippet": None,
        "lane": None,
    }
    if table_exists(conn, "messages"):
        cols = set(table_columns(conn, "messages"))
        mapping = {
            "thread_id": "thread_id" if "thread_id" in cols else None,
            "date": "date_utc" if "date_utc" in cols else ("date" if "date" in cols else None),
            "from": "from_addr" if "from_addr" in cols else ("from" if "from" in cols else None),
            "subject": "subject" if "subject" in cols else None,
            "snippet": "snippet" if "snippet" in cols else None,
            "lane": "lane" if "lane" in cols else None,
        }
        pieces = ["id AS message_id"]
        for key, col in mapping.items():
            if not col:
                continue
            src = '"from"' if col == "from" else col
            pieces.append("%s AS \"%s\"" % (src, key))
        row = conn.execute(
            "SELECT %s FROM messages WHERE id = ?" % ", ".join(pieces),
            (message_id,),
        ).fetchone()
        if row is not None:
            rec = dict(row)
            out["thread_id"] = rec.get("thread_id")
            out["date"] = rec.get("date")
            out["from"] = rec.get("from")
            out["subject"] = rec.get("subject")
            out["snippet"] = rec.get("snippet")
            out["lane"] = rec.get("lane")
    if table_exists(conn, "messages_fts"):
        fts_cols = set(table_columns(conn, "messages_fts"))
        fts = conn.execute(
            "SELECT * FROM messages_fts WHERE id = ?",
            (message_id,),
        ).fetchone()
        if fts is not None:
            rec = dict(fts)
            if not out["subject"] and rec.get("subject"):
                out["subject"] = rec.get("subject")
            if not out["from"] and rec.get("from_addr"):
                out["from"] = rec.get("from_addr")
            body = rec.get("body") if "body" in fts_cols else None
            if body and not out["snippet"]:
                out["snippet"] = el.snippet(str(body))
            elif body and out["snippet"] and len(str(out["snippet"])) < 8:
                out["snippet"] = el.snippet(str(body))
    if out["snippet"] is None and out["subject"]:
        out["snippet"] = el.snippet(str(out["subject"]))
    return out


def _subject_like_hits(
    conn: sqlite3.Connection,
    query: str,
    *,
    lane: str | None,
    after: str | None,
    before: str | None,
    k: int,
) -> list[str]:
    """Fallback when messages_fts has no subject column: scan messages.subject."""
    if not table_exists(conn, "messages"):
        return []
    cols = set(table_columns(conn, "messages"))
    if "subject" not in cols:
        return []
    tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
    if not tokens:
        return []
    extra, params = _message_filters_sql(
        conn, alias="m", lane=lane, after=after, before=before
    )
    likes = []
    like_params: list[Any] = []
    for tok in tokens:
        likes.append("LOWER(COALESCE(m.subject, '')) LIKE ?")
        like_params.append("%" + tok.lower().replace("%", "") + "%")
    sql = (
        "SELECT m.id AS id FROM messages m WHERE (%s)%s LIMIT ?"
        % (" AND ".join(likes), extra)
    )
    rows = conn.execute(sql, (*like_params, *params, int(k))).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = FTS_K,
    lane: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """FTS5 BM25 top-k on messages_fts. Subject-boost when the column exists."""
    if not table_exists(conn, "messages_fts"):
        return []
    fts_cols = table_columns(conn, "messages_fts")
    fts_set = set(fts_cols)
    match_q = fts_match_query(query)
    has_messages = table_exists(conn, "messages")
    join = "LEFT JOIN messages m ON m.id = fts.id" if has_messages else ""
    extra, params = ("", [])
    if has_messages:
        extra, params = _message_filters_sql(
            conn, alias="m", lane=lane, after=after, before=before
        )
    if "subject" in fts_set:
        bm25 = _bm25_weight_sql(fts_cols)
        sql = (
            "SELECT fts.id AS message_id, %s AS bm25 "
            "FROM messages_fts fts %s "
            "WHERE messages_fts MATCH ?%s "
            "ORDER BY %s LIMIT ?"
        ) % (bm25, join, extra, bm25)
        try:
            rows = conn.execute(sql, (match_q, *params, int(k))).fetchall()
        except sqlite3.Error:
            try:
                rows = conn.execute(
                    "SELECT fts.id AS message_id FROM messages_fts fts %s "
                    "WHERE messages_fts MATCH ?%s LIMIT ?" % (join, extra),
                    (match_q, *params, int(k)),
                ).fetchall()
            except sqlite3.Error:
                return []
        out: list[dict[str, Any]] = []
        for i, row in enumerate(rows, start=1):
            mid = row["message_id"] if isinstance(row, sqlite3.Row) else row[0]
            if mid:
                out.append({"message_id": str(mid), "fts_rank": i})
        return out

    # No subject column on FTS: MATCH remaining columns + separate subject scan.
    sql = (
        "SELECT fts.id AS message_id FROM messages_fts fts %s "
        "WHERE messages_fts MATCH ?%s LIMIT ?"
    ) % (join, extra)
    try:
        rows = conn.execute(sql, (match_q, *params, int(k))).fetchall()
    except sqlite3.Error:
        rows = []
    body_ids = [
        str(r["message_id"] if isinstance(r, sqlite3.Row) else r[0])
        for r in rows
        if (r["message_id"] if isinstance(r, sqlite3.Row) else r[0])
    ]
    subject_ids = _subject_like_hits(
        conn, query, lane=lane, after=after, before=before, k=k
    )
    merged: list[str] = []
    seen: set[str] = set()
    for mid in subject_ids + body_ids:
        if mid in seen:
            continue
        seen.add(mid)
        merged.append(mid)
        if len(merged) >= k:
            break
    return [{"message_id": mid, "fts_rank": i} for i, mid in enumerate(merged, start=1)]


def resolve_chunk_id(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    vec_rowid: int | None = None,
) -> str | None:
    """chunk_id from live chunk_vec_map (vec_rowid, else message_id). No invented cols."""
    if not table_exists(conn, "chunk_vec_map"):
        return None
    cols = set(table_columns(conn, "chunk_vec_map"))
    if "chunk_id" not in cols:
        return None
    if vec_rowid is not None and "vec_rowid" in cols:
        row = conn.execute(
            "SELECT chunk_id FROM chunk_vec_map WHERE vec_rowid = ? LIMIT 1",
            (int(vec_rowid),),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    if "message_id" in cols:
        row = conn.execute(
            "SELECT chunk_id FROM chunk_vec_map WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    return None


def vec_search(
    conn: sqlite3.Connection,
    query_vector: Sequence[float],
    *,
    k: int = VEC_K,
    dims: int = QUERY_DIMS,
    lane: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """sqlite-vec KNN top-k on live message_embeddings. Join chunk_vec_map when present."""
    if not table_exists(conn, "message_embeddings"):
        return []
    if len(query_vector) != dims:
        raise RetrieveError(
            "Query vector dim %s != %s (instruct_version=%s)."
            % (len(query_vector), dims, QUERY_INSTRUCT_VERSION)
        )
    vec_sql = (
        "SELECT v.message_id AS message_id, v.distance AS distance, v.rowid AS vec_rowid "
        "FROM message_embeddings AS v "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance"
    )
    try:
        rows = conn.execute(
            vec_sql, (el.serialize_f32(query_vector), max(1, int(k)))
        ).fetchall()
    except sqlite3.Error:
        return []

    lo, hi = _date_bounds(after, before)
    out: list[dict[str, Any]] = []
    rank = 0
    for row in rows:
        rec = dict(row)
        mid = rec.get("message_id")
        if not mid:
            continue
        meta = _load_message(conn, str(mid)) if (lane or lo or hi) else None
        if lane or lo or hi:
            assert meta is not None
            if lane and meta.get("lane") and str(meta["lane"]).lower() != lane.lower():
                continue
            if lane and not meta.get("lane"):
                continue
            date_s = str(meta.get("date") or "")
            if lo and date_s < lo:
                continue
            if hi and date_s > hi:
                continue
        rank += 1
        vec_rowid = rec.get("vec_rowid")
        try:
            vec_rowid_i = int(vec_rowid) if vec_rowid is not None else None
        except (TypeError, ValueError):
            vec_rowid_i = None
        out.append(
            {
                "message_id": str(mid),
                "vec_rank": rank,
                "chunk_id": resolve_chunk_id(
                    conn, str(mid), vec_rowid=vec_rowid_i
                ),
                "distance": rec.get("distance"),
            }
        )
        if rank >= k:
            break
    return out


def embed_query_vector(
    query: str,
    *,
    embed_fn: EmbedFn | None = None,
    query_vector: Sequence[float] | None = None,
    model: str = el.DEFAULT_MODEL,
    ollama_url: str = el.DEFAULT_OLLAMA_URL,
    dims: int = QUERY_DIMS,
) -> tuple[Sequence[float] | None, str | None]:
    """Embed query with v1 prefix + dims=1024. Returns (vector, warn)."""
    if query_vector is not None:
        if len(query_vector) != dims:
            raise RetrieveError(
                "Query vector dim %s != %s." % (len(query_vector), dims)
            )
        return query_vector, None
    q = (query or "").strip()
    if not q:
        return None, None
    payload = query_embed_text(q)
    worker = embed_fn or el.make_ollama_embed_fn(ollama_url, dims)
    try:
        vectors = worker([payload], model)
    except Exception as exc:  # fail-open vec side — FTS still ranks
        return None, "query embed failed (%s); vec_rank=%s" % (exc, MISSING_RANK)
    if not vectors:
        return None, "query embed returned no vector; vec_rank=%s" % MISSING_RANK
    adapted = el.adapt_dims(vectors[0], dims)
    return adapted, None


def rerank_hits(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """PR-7 live Qwen3-Reranker is out of scope. Fail-open: pass-through RRF order."""
    del query
    for hit in hits:
        hit["rerank"] = None
    return hits


def _thread_id_key(hit: dict[str, Any]) -> str:
    tid = hit.get("thread_id")
    if tid:
        return str(tid)
    return "msg:%s" % hit.get("message_id")


def _dedup_generator_pack(hits: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Keep the best-rrf Hit per thread_id (generator pack). Citations stay on Hits."""
    pack: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        key = _thread_id_key(hit)
        if key in seen:
            continue
        seen.add(key)
        pack.append(hit)
        if len(pack) >= k:
            break
    return pack


def _thread_messages(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "messages"):
        return []
    cols = set(table_columns(conn, "messages"))
    if "thread_id" not in cols:
        return []
    date_col = "date_utc" if "date_utc" in cols else ("date" if "date" in cols else "id")
    rows = conn.execute(
        "SELECT id FROM messages WHERE thread_id = ? ORDER BY %s ASC, id ASC"
        % date_col,
        (thread_id,),
    ).fetchall()
    return [_load_message(conn, str(r[0])) for r in rows if r[0]]


def expand_thread_spine(
    conn: sqlite3.Connection,
    pack: list[dict[str, Any]],
    ranked_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parent + thread spine: root + last 3 + other in-thread ranked hits.

    No-op when PR-2 ``thread_id`` is missing. Spine Hits keep citation fields
    when they were already ranked; otherwise missing ranks = 1000.
    """
    if not table_exists(conn, "messages"):
        return list(pack)
    if "thread_id" not in set(table_columns(conn, "messages")):
        return list(pack)

    extra: list[dict[str, Any]] = []
    pack_ids = {str(h.get("message_id")) for h in pack}
    seen_extra: set[str] = set(pack_ids)

    for hit in pack:
        tid = hit.get("thread_id")
        if not tid:
            continue
        members = _thread_messages(conn, str(tid))
        if not members:
            continue
        root = members[0]
        for mem in members:
            if str(mem.get("message_id")) == str(tid):
                root = mem
                break
        last3 = members[-3:]
        others = [
            ranked_by_id[mid]
            for mid in ranked_by_id
            if ranked_by_id[mid].get("thread_id") == tid and mid not in pack_ids
        ]
        spine_meta = [root, *last3, *others]
        for meta in spine_meta:
            mid = str(meta.get("message_id") or "")
            if not mid or mid in seen_extra:
                continue
            seen_extra.add(mid)
            ranked = ranked_by_id.get(mid)
            if ranked:
                extra.append(dict(ranked))
                continue
            loaded = meta if meta.get("subject") is not None else _load_message(conn, mid)
            extra.append(
                _empty_hit(
                    mid,
                    chunk_id=loaded.get("chunk_id"),
                    thread_id=loaded.get("thread_id") or tid,
                    date=loaded.get("date"),
                    **{"from": loaded.get("from")},
                    subject=loaded.get("subject"),
                    snippet=loaded.get("snippet"),
                    lane=loaded.get("lane"),
                    fts_rank=MISSING_RANK,
                    vec_rank=MISSING_RANK,
                    rrf=0.0,
                    rerank=None,
                )
            )
    return list(pack) + extra


def _public_hit(hit: dict[str, Any]) -> dict[str, Any]:
    return {key: hit.get(key) for key in HIT_FIELDS}


def retrieve(
    query: str,
    k: int = DEFAULT_K,
    lane: str | None = None,
    after: str | None = None,
    before: str | None = None,
    *,
    db: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
    embed_fn: EmbedFn | None = None,
    query_vector: Sequence[float] | None = None,
    vec_hits_fn: VecHitsFn | None = None,
    model: str = el.DEFAULT_MODEL,
    ollama_url: str = el.DEFAULT_OLLAMA_URL,
    dims: int = QUERY_DIMS,
    extension_path: str | None = None,
    now: datetime | None = None,
    expand_threads: bool = True,
) -> list[dict[str, Any]]:
    """Hybrid FTS + vec + RRF retrieve. See module docstring for filters."""
    q = (query or "").strip()
    if not q:
        raise RetrieveError("Query text is empty.")
    k = max(1, int(k))
    inferred = infer_lane(q, lane)
    date_window = bool(after or before)
    clock = now or utc_now()
    ident = mids.is_identifier_query(q)

    owns = False
    used = conn
    if used is None:
        path = Path(db).expanduser() if db is not None else default_db_path()
        if not path.is_file():
            raise RetrieveError("database not found: %s" % path)
        try:
            used = el.connect_db(path, extension_path)
        except Exception:
            used = sqlite3.connect(str(path))
            used.row_factory = sqlite3.Row
        owns = True
    assert used is not None
    try:
        def _run_vec(active_lane: str | None) -> list[dict[str, Any]]:
            if vec_hits_fn is not None:
                return list(
                    vec_hits_fn(
                        query=q,
                        k=VEC_K,
                        dims=dims,
                        lane=active_lane,
                        after=after,
                        before=before,
                    )
                    or []
                )
            vector, _warn = embed_query_vector(
                q,
                embed_fn=embed_fn,
                query_vector=query_vector,
                model=model,
                ollama_url=ollama_url,
                dims=dims,
            )
            if vector is None:
                return []
            return vec_search(
                used,
                vector,
                k=VEC_K,
                dims=dims,
                lane=active_lane,
                after=after,
                before=before,
            )

        fts_hits = fts_search(
            used, q, k=FTS_K, lane=inferred, after=after, before=before
        )
        ids_hits: list[dict[str, Any]] = []
        if ident:
            ids_hits = mids.match_messages_ids(used, q, k=IDS_K)
        vec_hits = _run_vec(inferred)
        # Inferred lane only: if nothing matched, fail-open without the lane
        # filter so Mac smoke (SDGE / Caddell) still ranks when live `lane`
        # values are not exactly money/people. Explicit --lane stays strict.
        inferred_only = lane is None and inferred is not None
        if inferred_only and not fts_hits and not vec_hits and not ids_hits:
            inferred = None
            fts_hits = fts_search(
                used, q, k=FTS_K, lane=None, after=after, before=before
            )
            vec_hits = _run_vec(None)

        merged: dict[str, dict[str, Any]] = {}

        def _bucket(message_id: str) -> dict[str, Any]:
            if message_id not in merged:
                meta = _load_message(used, message_id)
                chunk = resolve_chunk_id(used, message_id)
                merged[message_id] = _empty_hit(
                    message_id,
                    chunk_id=chunk or meta.get("chunk_id"),
                    thread_id=meta.get("thread_id"),
                    date=meta.get("date"),
                    **{"from": meta.get("from")},
                    subject=meta.get("subject"),
                    snippet=meta.get("snippet"),
                    lane=meta.get("lane"),
                )
            return merged[message_id]

        for item in fts_hits:
            rec = _bucket(item["message_id"])
            rec["fts_rank"] = int(item["fts_rank"])
        for item in vec_hits:
            rec = _bucket(item["message_id"])
            rec["vec_rank"] = int(item["vec_rank"])
            if item.get("chunk_id"):
                rec["chunk_id"] = item["chunk_id"]
        for item in ids_hits:
            rec = _bucket(item["message_id"])
            rec["_ids_rank"] = int(item["ids_rank"])

        ranked: list[dict[str, Any]] = []
        for rec in merged.values():
            rec["rrf"] = rrf_score(
                rec.get("fts_rank"),
                rec.get("vec_rank"),
                ids_rank=rec.get("_ids_rank"),
                include_ids=ident,
            )
            if not date_window:
                rec["rrf"] *= recency_multiplier(rec.get("date"), now=clock)
            rec["rerank"] = None
            ranked.append(rec)
        ranked.sort(key=lambda h: (-float(h["rrf"]), str(h.get("message_id"))))
        ranked = rerank_hits(ranked, q)
        pack = _dedup_generator_pack(ranked, k)
        ranked_by_id = {str(h["message_id"]): h for h in ranked}
        if expand_threads:
            hits = expand_thread_spine(used, pack, ranked_by_id)
        else:
            hits = pack
        return [_public_hit(h) for h in hits]
    finally:
        if owns:
            used.close()


def semantic_search(
    query: str,
    db: str | Path | None = None,
    **kwargs: Any,
) -> list[dict]:
    """Backward-compatible cosine path (embed_lib.semantic_search)."""
    path = Path(db).expanduser() if db is not None else default_db_path()
    return el.semantic_search(query, path, **kwargs)


def format_hits(hits: Iterable[dict]) -> str:
    """Pretty-print hybrid Hits (falls back to cosine fields)."""
    lines: list[str] = []
    for i, hit in enumerate(hits, 1):
        if "message_id" in hit and "rrf" in hit:
            lines.append(
                "%d. %s  rrf=%.6f  fts_rank=%s  vec_rank=%s  rerank=%s  lane=%s"
                % (
                    i,
                    hit.get("message_id"),
                    float(hit.get("rrf") or 0.0),
                    hit.get("fts_rank"),
                    hit.get("vec_rank"),
                    hit.get("rerank"),
                    hit.get("lane"),
                )
            )
            lines.append(
                "   thread=%s  chunk=%s  date=%s  from=%s"
                % (
                    hit.get("thread_id"),
                    hit.get("chunk_id"),
                    hit.get("date"),
                    hit.get("from"),
                )
            )
            lines.append("   subject: %s" % (hit.get("subject") or ""))
            lines.append("   snippet: %s" % (hit.get("snippet") or ""))
        else:
            lines.append(
                f"{i}. {hit.get('id')}  score={float(hit.get('score') or 0):.4f}  "
                f"distance={float(hit.get('distance') or 0):.4f}  "
                f"from={hit.get('from_addr')}"
            )
            lines.append(f"   subject: {hit.get('subject')}")
            lines.append(f"   snippet: {hit.get('snippet')}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "MAILROOM §6.2 hybrid retrieve (FTS + sqlite-vec + RRF). "
            "Default is retrieve(); --cosine keeps the old KNN path. "
            "Lane/date: FTS pre-filter, vec post-filter. "
            "Query embed instruct_version=v1 dims=1024."
        )
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="Search string (or: retrieve|cosine <query>).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"Mailroom SQLite path (default: {DEFAULT_DB}).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=None,
        help="Top-k generator-pack threads (default: 20 hybrid / 10 cosine).",
    )
    parser.add_argument(
        "--lane",
        default=None,
        help="Force lane (money|people|…). 'none' skips inference. Default: infer.",
    )
    parser.add_argument(
        "--after",
        default=None,
        help="Inclusive lower bound on messages.date_utc (ISO date or datetime).",
    )
    parser.add_argument(
        "--before",
        default=None,
        help="Upper bound on messages.date_utc (date-only includes that day).",
    )
    parser.add_argument(
        "--cosine",
        action="store_true",
        help="Backward-compatible cosine KNN only (embed_lib.semantic_search).",
    )
    parser.add_argument(
        "--model",
        default=el.DEFAULT_MODEL,
        help=f"Must match the backfill Ollama tag (default: {el.DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--ollama-url",
        default=el.DEFAULT_OLLAMA_URL,
        help=f"Local Ollama base URL (default: {el.DEFAULT_OLLAMA_URL}).",
    )
    parser.add_argument(
        "--dims",
        type=int,
        default=QUERY_DIMS,
        help=f"Stored / query dims (default: {QUERY_DIMS}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON lines (Hits).",
    )
    parser.add_argument(
        "--vec-extension",
        default=None,
        help="Path to vec0.dylib / vec0.so if pip sqlite-vec is not installed.",
    )
    parser.add_argument(
        "--no-thread-expand",
        action="store_true",
        help="Skip thread-spine expansion (pack Hits only).",
    )
    return parser


def _split_query_tokens(tokens: list[str]) -> tuple[str, bool]:
    if tokens and tokens[0] in ("retrieve", "hybrid"):
        return " ".join(tokens[1:]).strip(), False
    if tokens and tokens[0] in ("cosine", "semantic"):
        return " ".join(tokens[1:]).strip(), True
    return " ".join(tokens).strip(), False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    query, sub_cosine = _split_query_tokens(list(args.query))
    cosine = bool(args.cosine or sub_cosine)
    if not query:
        sys.stderr.write("error: query text is empty\n")
        return 2
    db = Path(args.db).expanduser() if args.db else default_db_path()
    k = args.k if args.k is not None else (COSINE_K if cosine else DEFAULT_K)
    try:
        if cosine:
            hits = semantic_search(
                query,
                db,
                k=k,
                model=args.model,
                ollama_url=args.ollama_url,
                dims=args.dims,
                extension_path=args.vec_extension,
            )
        else:
            hits = retrieve(
                query,
                k=k,
                lane=args.lane,
                after=args.after,
                before=args.before,
                db=db,
                model=args.model,
                ollama_url=args.ollama_url,
                dims=args.dims,
                extension_path=args.vec_extension,
                expand_threads=not args.no_thread_expand,
            )
    except (RetrieveError, el.EmbedError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if not hits:
        sys.stdout.write(
            "no hits (FTS empty / embeddings not backfilled; "
            "identifier backfill: python scripts/messages_ids.py --backfill)\n"
        )
        return 0
    if args.json:
        for hit in hits:
            sys.stdout.write(json.dumps(hit, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(format_hits(hits) + "\n")
        if cosine:
            sys.stderr.write(
                "\n# cosine KNN only — hybrid retrieve is the default CLI.\n"
            )
        else:
            sys.stderr.write(
                "\n# hybrid retrieve: FTS pre-filter + vec post-filter; "
                "rerank=None (PR-7). instruct_version=%s dims=%s\n"
                % (QUERY_INSTRUCT_VERSION, QUERY_DIMS)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
