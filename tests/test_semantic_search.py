#!/usr/bin/env python3
"""PR-6 hybrid retrieve() tests. Temp DB / mocks — no live SoR, no Ollama."""

from __future__ import annotations

import io
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402
import messages_ids as mids  # noqa: E402
import semantic_search as ss  # noqa: E402


NOW = datetime(2026, 9, 6, 15, 0, 0, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE messages (
          id TEXT PRIMARY KEY,
          source TEXT,
          date_utc TEXT,
          from_addr TEXT,
          from_name TEXT,
          subject TEXT,
          snippet TEXT,
          lane TEXT,
          message_id_header TEXT,
          in_reply_to TEXT,
          thread_id TEXT,
          references_header TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
          id UNINDEXED, subject, body, from_addr, tokenize='porter unicode61'
        );
        CREATE TABLE chunk_vec_map (
          chunk_id TEXT PRIMARY KEY,
          vec_rowid INTEGER UNIQUE,
          message_id TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE messages_ids USING fts5(
          id UNINDEXED, message_id, tokenize='unicode61'
        );
        """
    )
    return conn


def _add(
    conn: sqlite3.Connection,
    mid: str,
    *,
    subject: str,
    body: str,
    from_addr: str = "a@example.com",
    date_utc: str = "2026-09-01T00:00:00Z",
    lane: str = "inbox",
    thread_id: str | None = None,
    message_id_header: str | None = None,
    snippet: str | None = None,
    chunk_id: str | None = None,
    identifiers: list[str] | None = None,
) -> None:
    tid = thread_id or mid
    header = message_id_header or ("<%s@x>" % mid)
    conn.execute(
        """
        INSERT INTO messages(
          id, source, date_utc, from_addr, subject, snippet, lane,
          message_id_header, thread_id
        ) VALUES (?, 'dump', ?, ?, ?, ?, ?, ?, ?)
        """,
        (mid, date_utc, from_addr, subject, snippet or subject, lane, header, tid),
    )
    conn.execute(
        "INSERT INTO messages_fts(id, subject, body, from_addr) VALUES (?, ?, ?, ?)",
        (mid, subject, body, from_addr),
    )
    if chunk_id:
        conn.execute(
            "INSERT INTO chunk_vec_map(chunk_id, vec_rowid, message_id) VALUES (?, ?, ?)",
            (chunk_id, abs(hash(mid)) % 100000, mid),
        )
    tokens = list(identifiers or [])
    tokens.extend(mids.extract_identifiers(subject, body, header, mid))
    mids.upsert_ids_row(conn, mid, message_id=header, identifiers=tokens)
    conn.commit()


def _vec_fn(order: list[str]):
    def _fn(**_kwargs):
        return [
            {"message_id": mid, "vec_rank": i, "chunk_id": None}
            for i, mid in enumerate(order, start=1)
        ]

    return _fn


class LaneInferTests(unittest.TestCase):
    def test_money_words(self):
        self.assertEqual(ss.infer_lane("SDGE bill due Friday"), "money")
        self.assertEqual(ss.infer_lane("please pay this invoice"), "money")

    def test_people_title_case(self):
        self.assertEqual(ss.infer_lane("Caddell"), "people")
        self.assertEqual(ss.infer_lane("email from Ada Lovelace"), "people")

    def test_none_and_explicit(self):
        self.assertIsNone(ss.infer_lane("harbor news tomorrow"))
        self.assertEqual(ss.infer_lane("Caddell", explicit="money"), "money")
        self.assertIsNone(ss.infer_lane("invoice", explicit="none"))

    def test_money_wins_over_people(self):
        self.assertEqual(ss.infer_lane("invoice from Caddell"), "money")


class RrfTests(unittest.TestCase):
    def test_missing_rank_is_1000(self):
        expected = 1.0 / 61.0 + 1.0 / 1060.0
        self.assertAlmostEqual(ss.rrf_score(1, None), expected)
        self.assertAlmostEqual(ss.rrf_score(None, 1), expected)

    def test_ids_term_only_when_requested(self):
        base = ss.rrf_score(1, 2)
        with_ids = ss.rrf_score(1, 2, ids_rank=1, include_ids=True)
        self.assertGreater(with_ids, base)
        self.assertAlmostEqual(with_ids, base + 1.0 / 61.0)

    def test_recency_decay(self):
        old = "2026-08-07T15:00:00Z"  # 30 days before NOW
        mult = ss.recency_multiplier(old, now=NOW)
        self.assertAlmostEqual(mult, math.exp(-0.002 * 30.0), places=8)


class QueryEmbedTests(unittest.TestCase):
    def test_v1_prefix_and_1024_dims(self):
        seen: list[tuple[list[str], str]] = []

        def fake_embed(texts, model):
            seen.append((list(texts), model))
            return [[0.0] * 1024]

        vector, warn = ss.embed_query_vector(
            "SDGE bill", embed_fn=fake_embed, dims=1024
        )
        self.assertIsNone(warn)
        self.assertEqual(len(vector), 1024)
        self.assertEqual(ss.QUERY_INSTRUCT_VERSION, "v1")
        self.assertEqual(ss.QUERY_DIMS, 1024)
        self.assertTrue(seen[0][0][0].startswith(el.QUERY_INSTRUCT))
        self.assertIn("SDGE bill", seen[0][0][0])
        self.assertEqual(ss.query_embed_text("hi"), el.query_text("hi"))


class RetrieveHitTests(unittest.TestCase):
    def test_hits_have_ranks_and_rrf(self):
        conn = _conn()
        _add(
            conn,
            "m-sdge",
            subject="SDGE bill ready",
            body="Your San Diego Gas electric invoice is due",
            lane="money",
            date_utc="2026-09-05T00:00:00Z",
            chunk_id="chunk-sdge-0",
        )
        _add(
            conn,
            "m-boat",
            subject="Harbor news",
            body="sailboat mooring fees",
            lane="inbox",
            date_utc="2026-01-01T00:00:00Z",
        )
        hits = ss.retrieve(
            "SDGE bill",
            k=5,
            lane="none",
            conn=conn,
            vec_hits_fn=_vec_fn(["m-sdge", "m-boat"]),
            now=NOW,
            expand_threads=False,
        )
        self.assertGreaterEqual(len(hits), 1)
        top = hits[0]
        for key in ss.HIT_FIELDS:
            self.assertIn(key, top, key)
        self.assertEqual(top["message_id"], "m-sdge")
        self.assertEqual(top["fts_rank"], 1)
        self.assertEqual(top["vec_rank"], 1)
        self.assertIsNone(top["rerank"])
        self.assertGreater(top["rrf"], 0)
        self.assertEqual(top["chunk_id"], "chunk-sdge-0")
        expected = ss.rrf_score(1, 1) * ss.recency_multiplier(
            "2026-09-05T00:00:00Z", now=NOW
        )
        self.assertAlmostEqual(float(top["rrf"]), expected, places=8)

    def test_date_window_skips_recency_and_prefilters_fts(self):
        conn = _conn()
        _add(
            conn,
            "old",
            subject="SDGE bill 2020",
            body="old electric invoice",
            lane="money",
            date_utc="2020-01-01T00:00:00Z",
        )
        _add(
            conn,
            "new",
            subject="SDGE bill 2026",
            body="new electric invoice",
            lane="money",
            date_utc="2026-08-01T00:00:00Z",
        )
        hits = ss.retrieve(
            "SDGE invoice",
            k=5,
            lane="money",
            after="2026-01-01",
            before="2026-12-31",
            conn=conn,
            vec_hits_fn=_vec_fn([]),
            now=NOW,
            expand_threads=False,
        )
        ids = [h["message_id"] for h in hits]
        self.assertIn("new", ids)
        self.assertNotIn("old", ids)
        self.assertAlmostEqual(hits[0]["rrf"], ss.rrf_score(1, None), places=8)

    def test_explicit_lane_filters(self):
        conn = _conn()
        _add(conn, "p1", subject="Caddell notes", body="call Caddell", lane="people")
        _add(conn, "m1", subject="Caddell invoice", body="pay Caddell", lane="money")
        hits = ss.retrieve(
            "Caddell",
            k=5,
            lane="people",
            conn=conn,
            vec_hits_fn=_vec_fn(["p1", "m1"]),
            now=NOW,
            expand_threads=False,
        )
        ids = [h["message_id"] for h in hits]
        self.assertEqual(ids, ["p1"])

    def test_inferred_lane_fail_open_when_empty(self):
        conn = _conn()
        _add(
            conn,
            "m1",
            subject="SDGE bill",
            body="utility invoice",
            lane="inbox",
        )
        hits = ss.retrieve(
            "SDGE bill",
            k=5,
            conn=conn,
            vec_hits_fn=_vec_fn([]),
            now=NOW,
            expand_threads=False,
        )
        self.assertEqual(hits[0]["message_id"], "m1")

    def test_identifier_uses_messages_ids(self):
        conn = _conn()
        _add(
            conn,
            "inv1",
            subject="Invoice INV-7788 posted",
            body="please remit INV-7788 for parcel 123-456-78",
            lane="money",
            identifiers=["INV-7788", "123-456-78"],
        )
        _add(
            conn,
            "other",
            subject="random hello",
            body="no numbers here",
            lane="inbox",
        )
        self.assertTrue(mids.is_identifier_query("INV-7788"))
        self.assertTrue(mids.is_identifier_query("123-456-78"))
        hits = ss.retrieve(
            "INV-7788",
            k=5,
            lane="none",
            conn=conn,
            vec_hits_fn=_vec_fn([]),
            now=NOW,
            expand_threads=False,
        )
        self.assertEqual(hits[0]["message_id"], "inv1")
        # Identifier term must beat a missing-ids peer.
        uuid_q = "550e8400-e29b-41d4-a716-446655440000"
        _add(
            conn,
            "u1",
            subject="ticket",
            body="ref 550e8400-e29b-41d4-a716-446655440000",
            lane="inbox",
        )
        self.assertTrue(mids.is_identifier_query(uuid_q))
        uhits = ss.retrieve(
            uuid_q,
            k=5,
            lane="none",
            conn=conn,
            vec_hits_fn=_vec_fn([]),
            now=NOW,
            expand_threads=False,
        )
        self.assertEqual(uhits[0]["message_id"], "u1")

    def test_rerank_stub_fail_open(self):
        hits = [
            {"message_id": "b", "rrf": 0.1, "rerank": 99},
            {"message_id": "a", "rrf": 0.2, "rerank": 1},
        ]
        out = ss.rerank_hits(hits, "q")
        self.assertEqual([h["message_id"] for h in out], ["b", "a"])
        self.assertTrue(all(h["rerank"] is None for h in out))

    def test_thread_dedup_and_spine(self):
        conn = _conn()
        _add(
            conn,
            "root",
            subject="Project kickoff",
            body="starting the thread",
            thread_id="root",
            date_utc="2026-01-01T00:00:00Z",
            lane="inbox",
        )
        for i, day in enumerate(("02", "03", "04", "05"), start=1):
            _add(
                conn,
                "r%s" % i,
                subject="Re: Project kickoff %s" % i,
                body="reply %s with invoice chatter" % i,
                thread_id="root",
                date_utc="2026-01-%sT00:00:00Z" % day,
                lane="inbox",
            )
        _add(
            conn,
            "solo",
            subject="Unrelated invoice",
            body="different invoice thread",
            thread_id="solo",
            date_utc="2026-02-01T00:00:00Z",
            lane="money",
        )
        hits = ss.retrieve(
            "invoice",
            k=2,
            lane="none",
            conn=conn,
            vec_hits_fn=_vec_fn(["r4", "solo", "r3", "r2"]),
            now=NOW,
            expand_threads=True,
        )
        pack_ids = []
        seen = set()
        for hit in hits:
            key = hit["thread_id"] or hit["message_id"]
            if key in seen:
                continue
            seen.add(key)
            pack_ids.append(hit["message_id"])
            if len(pack_ids) >= 2:
                break
        self.assertEqual(len(pack_ids), 2)
        all_ids = [h["message_id"] for h in hits]
        self.assertIn("root", all_ids)
        self.assertTrue(any(h.get("thread_id") == "root" for h in hits))

    def test_chunk_vec_map_join(self):
        conn = _conn()
        _add(conn, "m1", subject="x", body="y", chunk_id="c-m1-0")
        self.assertEqual(ss.resolve_chunk_id(conn, "m1"), "c-m1-0")
        self.assertIsNone(ss.resolve_chunk_id(conn, "missing"))

    def test_subject_boost_fallback_without_subject_column(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE messages (
              id TEXT PRIMARY KEY, subject TEXT, date_utc TEXT, lane TEXT, from_addr TEXT
            );
            CREATE VIRTUAL TABLE messages_fts USING fts5(
              id UNINDEXED, body, tokenize='porter unicode61'
            );
            INSERT INTO messages VALUES
              ('s1', 'UniqueSubjectZebra', '2026-09-01T00:00:00Z', 'inbox', 'a@x'),
              ('b1', 'Other', '2026-09-01T00:00:00Z', 'inbox', 'a@x');
            INSERT INTO messages_fts VALUES
              ('s1', 'unrelated body text'),
              ('b1', 'mentions UniqueSubjectZebra in the body');
            """
        )
        hits = ss.fts_search(conn, "UniqueSubjectZebra", k=10)
        ids = [h["message_id"] for h in hits]
        self.assertEqual(ids[0], "s1")


class MessagesIdsTests(unittest.TestCase):
    def test_additive_identifiers_column(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE messages_ids USING fts5("
            "id UNINDEXED, message_id, tokenize='unicode61')"
        )
        conn.execute(
            "INSERT INTO messages_ids(id, message_id) VALUES ('m1', '<a@x>')"
        )
        self.assertNotIn("identifiers", mids.columns(conn, "messages_ids"))
        action = mids.ensure_messages_ids(conn)
        self.assertEqual(action, "added_identifiers")
        self.assertIn("identifiers", mids.columns(conn, "messages_ids"))
        row = conn.execute("SELECT id, message_id FROM messages_ids").fetchone()
        self.assertEqual(row[0], "m1")
        self.assertEqual(row[1], "<a@x>")

    def test_extract_and_backfill(self):
        conn = _conn()
        _add(
            conn,
            "m1",
            subject="APN 123-456-78",
            body="invoice INV-99 uuid 550e8400-e29b-41d4-a716-446655440000",
        )
        report = mids.backfill_identifiers(conn)
        self.assertGreaterEqual(report["updated"], 1)
        cols = mids.columns(conn, "messages_ids")
        self.assertIn("identifiers", cols)
        row = conn.execute(
            "SELECT identifiers FROM messages_ids WHERE id='m1'"
        ).fetchone()
        text = row[0] or ""
        self.assertIn("123-456-78", text)
        self.assertIn("550e8400-e29b-41d4-a716-446655440000", text)

    def test_ci_refuses_default_sor(self):
        with mock.patch.dict("os.environ", {"CI": "1"}, clear=False):
            with self.assertRaises(mids.MessagesIdsError):
                mids._ci_refuses_default_sor(mids.DEFAULT_DB)


class CliTests(unittest.TestCase):
    def test_help_documents_hybrid_and_filters(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "semantic_search.py"), "--help"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--lane", proc.stdout)
        self.assertIn("--after", proc.stdout)
        self.assertIn("--before", proc.stdout)
        self.assertIn("--cosine", proc.stdout)
        self.assertIn("v1", proc.stdout)
        self.assertIn("1024", proc.stdout)
        self.assertIn("pre-filter", proc.stdout)

    def test_cli_json_retrieve_mocked(self):
        fake = [
            {
                "message_id": "m1",
                "chunk_id": None,
                "thread_id": "t1",
                "date": "2026-09-01T00:00:00Z",
                "from": "a@x",
                "subject": "SDGE bill",
                "snippet": "due",
                "fts_rank": 1,
                "vec_rank": 2,
                "rrf": 0.017,
                "rerank": None,
                "lane": "money",
            }
        ]
        stdout = io.StringIO()
        with mock.patch.object(ss, "retrieve", return_value=fake):
            with mock.patch("sys.stdout", stdout):
                rc = ss.main(["--json", "--k", "5", "SDGE bill"])
        self.assertEqual(rc, 0)
        row = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(row["message_id"], "m1")
        self.assertEqual(row["fts_rank"], 1)
        self.assertEqual(row["vec_rank"], 2)
        self.assertIn("rrf", row)

    def test_cli_cosine_uses_legacy_path(self):
        fake = [
            {
                "id": "hit-apple",
                "subject": "Your Apple receipt",
                "from_addr": "receipts@apple.example",
                "score": 0.99,
                "distance": 0.01,
                "snippet": "apples",
            }
        ]
        stdout = io.StringIO()
        with mock.patch.object(ss, "semantic_search", return_value=fake):
            with mock.patch("sys.stdout", stdout):
                rc = ss.main(["--cosine", "--json", "apple receipt"])
        self.assertEqual(rc, 0)
        row = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(row["id"], "hit-apple")

    def test_ids_cli_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "messages_ids.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--backfill", proc.stdout)


if __name__ == "__main__":
    unittest.main()
