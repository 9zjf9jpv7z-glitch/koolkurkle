#!/usr/bin/env python3
"""Schema + fake-vector insert/query. No network."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402
from mailroom_test_util import insert_message, one_hot, open_mem  # noqa: E402


class SchemaTests(unittest.TestCase):
    def test_schema_file_declares_1024_and_meta(self):
        sql = (SCRIPTS / "embed_schema.sql").read_text(encoding="utf-8")
        self.assertIn("embedding float[1024]", sql)
        self.assertNotIn("embedding float[1536]", sql)
        self.assertIn("distance_metric=cosine", sql)
        self.assertIn("embedding_meta", sql)
        self.assertIn("text_hash", sql)
        self.assertIn("qwen3-embedding-8b", sql)
        self.assertIn("message_id TEXT PRIMARY KEY", sql)
        self.assertIn("dims INTEGER", sql)

    def test_apply_schema_creates_tables(self):
        conn = open_mem()
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('embedding_meta', 'message_embeddings', 'messages', 'messages_fts')"
            )
        }
        self.assertEqual(
            names,
            {"embedding_meta", "message_embeddings", "messages", "messages_fts"},
        )
        conn.close()

    def test_apply_schema_is_idempotent(self):
        conn = open_mem()
        el.apply_schema(conn)
        el.apply_schema(conn)
        conn.close()

    def test_insert_and_knn_fake_vector(self):
        conn = open_mem()
        insert_message(conn, "msg-a", subject="Apples", body="apple orchard cider")
        insert_message(conn, "msg-b", subject="Boats", body="harbor sailboat")
        el.upsert_embedding(
            conn,
            message_id="msg-a",
            vector=one_hot(0),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash="aaa",
            char_count=10,
        )
        el.upsert_embedding(
            conn,
            message_id="msg-b",
            vector=one_hot(1),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash="bbb",
            char_count=10,
        )
        conn.commit()
        query = [0.95, 0.05] + [0.0] * (el.DEFAULT_DIMS - 2)
        hits = el.knn_search(conn, query, k=2)
        self.assertEqual(hits[0]["id"], "msg-a")
        self.assertEqual(hits[0]["subject"], "Apples")
        self.assertIn("cider", hits[0]["snippet"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertEqual(hits[1]["id"], "msg-b")
        conn.close()

    def test_wrong_dimension_rejected(self):
        conn = open_mem()
        with self.assertRaises(el.EmbedError):
            el.upsert_embedding(
                conn,
                message_id="x",
                vector=[0.1, 0.2],
                model=el.DEFAULT_MODEL,
                model_version="v1",
                text_hash="h",
                char_count=1,
            )
        conn.close()

    def test_existing_1536_schema_rejected(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        el.load_sqlite_vec(conn)
        conn.executescript(
            """
            CREATE VIRTUAL TABLE message_embeddings USING vec0(
              message_id TEXT PRIMARY KEY,
              embedding float[1536] distance_metric=cosine
            );
            """
        )
        conn.commit()
        with self.assertRaises(el.EmbedError) as ctx:
            el.apply_schema(conn, dims=1024)
        self.assertIn("1536", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_embeddings'"
        ).fetchone()[0])
        self.assertIn("different dimension", str(ctx.exception))
        self.assertIn("DROP TABLE", str(ctx.exception))
        conn.close()

    def test_existing_messages_table_not_rewritten(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        el.load_sqlite_vec(conn)
        conn.execute(
            """
            CREATE TABLE messages (
              id TEXT PRIMARY KEY,
              source TEXT,
              folder TEXT,
              lane TEXT,
              junk INTEGER,
              uid INTEGER,
              present_on_server INTEGER,
              extra_keep TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE messages_fts USING fts5(
              id UNINDEXED, subject, body, from_addr,
              tokenize='porter unicode61'
            )
            """
        )
        conn.execute(
            "INSERT INTO messages(id, source, extra_keep) VALUES ('keep-1', 'dump', 'stay')"
        )
        conn.commit()
        el.apply_schema(conn)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
        self.assertIn("extra_keep", cols)
        self.assertEqual(
            conn.execute("SELECT extra_keep FROM messages WHERE id='keep-1'").fetchone()[0],
            "stay",
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
