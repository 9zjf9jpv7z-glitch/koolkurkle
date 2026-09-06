#!/usr/bin/env python3
"""PR-2 incremental §6.1 path. Temp DB only — no sqlite-vec, no Ollama, no SoR."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_backfill as eb  # noqa: E402
import embed_lib as el  # noqa: E402
import mail_clean as mc  # noqa: E402
import migrate_pr1_schema as mig  # noqa: E402
from embed_document import document_embed_text  # noqa: E402
from test_migrate_pr1_schema import apply_pr0_live_schema_minus_vec0  # noqa: E402


def incremental_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_pr0_live_schema_minus_vec0(conn)
    conn.execute(
        "CREATE TABLE message_embeddings ("
        "message_id TEXT PRIMARY KEY, embedding BLOB)"
    )
    mig.migrate(conn)
    conn.commit()
    return conn


def insert_msg(
    conn: sqlite3.Connection,
    mid: str,
    *,
    subject: str = "Hello",
    body: str = "New line.\n> quoted",
    lane: str = "inbox",
    from_addr: str = "ada@example.com",
    from_name: str = "Ada",
    to_addrs: str = "bob@example.com",
    date_utc: str = "2026-09-05T20:00:00Z",
    message_id_header: str = "<this@x>",
    in_reply_to: str | None = "<parent@x>",
    references_header: str | None = "<root@x> <parent@x>",
) -> None:
    conn.execute(
        """
        INSERT INTO messages(
          id, source, folder, date_utc, from_addr, from_name, to_addrs,
          subject, lane, message_id_header, in_reply_to, references_header
        ) VALUES (?, 'dump', 'INBOX', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mid,
            date_utc,
            from_addr,
            from_name,
            to_addrs,
            subject,
            lane,
            message_id_header,
            in_reply_to,
            references_header,
        ),
    )
    conn.execute(
        "INSERT INTO messages_fts(id, subject, body, from_addr) VALUES (?, ?, ?, ?)",
        (mid, subject, body, from_addr),
    )
    conn.commit()


def insert_meta(
    conn: sqlite3.Connection,
    mid: str,
    *,
    quote_stripped: int | None = 0,
    content_hash: str | None = None,
    text_hash: str = "abc",
) -> None:
    conn.execute(
        """
        INSERT INTO embedding_meta(
          message_id, model, model_version, created_at, text_hash,
          char_count, dims, quote_stripped, content_hash
        ) VALUES (?, 'qwen3-embedding-8b', 'v1', '2026-01-01T00:00:00+00:00',
                  ?, 10, 1024, ?, ?)
        """,
        (mid, text_hash, quote_stripped, content_hash),
    )
    conn.commit()


def fake_embed(texts, model):
    return [[0.0] * 1023 + [1.0] for _ in texts]


class IncrementalActionTests(unittest.TestCase):
    def test_missing_is_due(self):
        self.assertEqual(
            el.incremental_action(
                meta_present=False,
                quote_stripped=None,
                stored_hash=None,
                new_hash="aa",
            ),
            "missing",
        )

    def test_legacy_rem_row_is_skipped(self):
        self.assertEqual(
            el.incremental_action(
                meta_present=True,
                quote_stripped=0,
                stored_hash=None,
                new_hash="aa",
            ),
            "skip",
        )

    def test_quote_stripped_matching_hash_skipped(self):
        self.assertEqual(
            el.incremental_action(
                meta_present=True,
                quote_stripped=1,
                stored_hash="aa",
                new_hash="aa",
            ),
            "skip",
        )

    def test_stale_content_hash_is_due(self):
        self.assertEqual(
            el.incremental_action(
                meta_present=True,
                quote_stripped=1,
                stored_hash="old",
                new_hash="new",
            ),
            "stale",
        )


class CandidateTests(unittest.TestCase):
    def test_selects_missing_and_stale_skips_rem_and_fresh(self):
        conn = incremental_conn()
        insert_msg(conn, "missing")
        insert_msg(conn, "rem-owned")
        insert_msg(conn, "fresh")
        insert_msg(conn, "stale")
        insert_meta(conn, "rem-owned", quote_stripped=0, content_hash=None)
        fresh_hash = mc.content_hash(mc.clean_body("New line.\n> quoted"))
        insert_meta(conn, "fresh", quote_stripped=1, content_hash=fresh_hash)
        insert_meta(conn, "stale", quote_stripped=1, content_hash="deadbeef")
        rows = el.iter_incremental_candidates(conn)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["missing", "stale"])
        self.assertEqual(rows[0]["action"], "missing")
        self.assertEqual(rows[1]["action"], "stale")
        counts = el.incremental_candidate_counts(conn)
        self.assertEqual(counts["candidates"], 2)
        self.assertEqual(counts["skipped_legacy_embedded"], 1)
        self.assertEqual(counts["skipped_quote_stripped"], 1)
        self.assertEqual(counts["reembed_stale_content_hash"], 1)

    def test_skips_auth(self):
        conn = incremental_conn()
        insert_msg(conn, "auth-1", lane="auth")
        rows = el.iter_incremental_candidates(conn)
        self.assertEqual(rows, [])

    def test_max_chars_uses_header_prefixed_document(self):
        conn = incremental_conn()
        insert_msg(conn, "longish", body="x" * 200)
        doc_len = len(
            document_embed_text(
                subject="Hello",
                from_addr="ada@example.com",
                from_name="Ada",
                to_addrs="bob@example.com",
                date_iso="2026-09-05T20:00:00Z",
                lane="inbox",
                cleaned_body="x" * 200,
                cap=el.CHAR_CAP,
            )
        )
        rows = el.iter_incremental_candidates(conn, max_chars=doc_len - 1)
        self.assertEqual(rows, [])
        rows = el.iter_incremental_candidates(conn, max_chars=doc_len)
        self.assertEqual([r["id"] for r in rows], ["longish"])


class BackfillIncrementalTests(unittest.TestCase):
    def test_writes_clean_thread_meta_and_leaves_rem_row(self):
        conn = incremental_conn()
        insert_msg(conn, "new-1")
        insert_msg(conn, "rem-1")
        insert_meta(conn, "rem-1", quote_stripped=0, content_hash=None)
        conn.execute(
            "INSERT INTO message_embeddings(message_id, embedding) VALUES ('rem-1', X'00')"
        )
        conn.commit()
        logs: list[str] = []
        counts = el.backfill(
            conn,
            quote_strip=True,
            embed_fn=fake_embed,
            log=logs.append,
        )
        self.assertEqual(counts["embedded"], 1)
        self.assertEqual(counts["skipped_legacy_embedded"], 1)
        row = conn.execute("SELECT * FROM messages WHERE id='new-1'").fetchone()
        self.assertEqual(row["cleaned_body"], "New line.")
        self.assertEqual(row["cleaned_chars"], len("New line."))
        self.assertEqual(row["thread_id"], "<root@x>")
        self.assertEqual(row["in_reply_to"], "<parent@x>")
        self.assertEqual(row["references_header"], "<root@x> <parent@x>")
        self.assertEqual(row["content_hash"], mc.content_hash("New line."))
        fts = conn.execute(
            "SELECT body FROM messages_fts WHERE id='new-1'"
        ).fetchone()
        self.assertIn("> quoted", fts["body"])
        meta = conn.execute(
            "SELECT * FROM embedding_meta WHERE message_id='new-1'"
        ).fetchone()
        self.assertEqual(meta["quote_stripped"], 1)
        self.assertEqual(meta["embed_model"], "qwen3-embedding:8b")
        self.assertEqual(meta["embed_dim"], 1024)
        self.assertEqual(meta["instruct_version"], "v1")
        self.assertEqual(meta["content_hash"], mc.content_hash("New line."))
        self.assertEqual(meta["model"], "qwen3-embedding-8b")
        rem_msg = conn.execute("SELECT cleaned_body FROM messages WHERE id='rem-1'").fetchone()
        self.assertIsNone(rem_msg["cleaned_body"])
        rem_meta = conn.execute(
            "SELECT quote_stripped, content_hash FROM embedding_meta WHERE message_id='rem-1'"
        ).fetchone()
        self.assertEqual(int(rem_meta["quote_stripped"] or 0), 0)
        self.assertIsNone(rem_meta["content_hash"])
        rem_vec = conn.execute(
            "SELECT embedding FROM message_embeddings WHERE message_id='rem-1'"
        ).fetchone()
        self.assertEqual(bytes(rem_vec["embedding"]), b"\x00")
        self.assertTrue(any("quote_strip=1" in line for line in logs))

    def test_second_pass_skips_matching_quote_stripped(self):
        conn = incremental_conn()
        insert_msg(conn, "new-1")
        el.backfill(conn, quote_strip=True, embed_fn=fake_embed, log=lambda _m: None)
        counts = el.backfill(
            conn, quote_strip=True, embed_fn=fake_embed, log=lambda _m: None
        )
        self.assertEqual(counts["embedded"], 0)
        self.assertEqual(counts["candidates"], 0)
        self.assertEqual(counts["skipped_quote_stripped"], 1)

    def test_stale_content_hash_reembeds(self):
        conn = incremental_conn()
        insert_msg(conn, "stale-1")
        insert_meta(conn, "stale-1", quote_stripped=1, content_hash="deadbeef")
        counts = el.backfill(
            conn, quote_strip=True, embed_fn=fake_embed, log=lambda _m: None
        )
        self.assertEqual(counts["embedded"], 1)
        meta = conn.execute(
            "SELECT content_hash, quote_stripped FROM embedding_meta WHERE message_id='stale-1'"
        ).fetchone()
        self.assertEqual(meta["content_hash"], mc.content_hash("New line."))
        self.assertEqual(meta["quote_stripped"], 1)

    def test_refuses_non_1024_dims(self):
        conn = incremental_conn()
        insert_msg(conn, "n1")
        with self.assertRaises(el.EmbedError) as ctx:
            el.backfill(
                conn,
                quote_strip=True,
                dims=4096,
                dry_run=True,
                embed_fn=fake_embed,
            )
        self.assertIn("no vec0 rebuild", str(ctx.exception))

    def test_refuses_missing_pr1_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_pr0_live_schema_minus_vec0(conn)
        conn.execute(
            "CREATE TABLE message_embeddings (message_id TEXT PRIMARY KEY, embedding BLOB)"
        )
        with self.assertRaises(el.EmbedError) as ctx:
            el.require_incremental_schema(conn)
        self.assertIn("PR-1 columns missing", str(ctx.exception))

    def test_does_not_create_vec0_on_quote_strip(self):
        conn = incremental_conn()
        insert_msg(conn, "n1")
        sql_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_embeddings'"
        ).fetchone()[0]
        el.backfill(conn, quote_strip=True, embed_fn=fake_embed, log=lambda _m: None)
        sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_embeddings'"
        ).fetchone()[0]
        self.assertEqual(sql_before, sql_after)
        self.assertNotIn("vec0", sql_after.lower())


class OldPathUnchangedTests(unittest.TestCase):
    def test_iter_candidates_still_uses_subject_body(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        el.ensure_mailroom_tables(conn)
        conn.execute(
            """
            CREATE TABLE embedding_meta (
              message_id TEXT NOT NULL, model TEXT NOT NULL,
              model_version TEXT NOT NULL DEFAULT 'v1',
              created_at TEXT NOT NULL, text_hash TEXT NOT NULL,
              char_count INTEGER NOT NULL DEFAULT 0,
              dims INTEGER NOT NULL DEFAULT 1024,
              PRIMARY KEY (message_id, model, model_version)
            )
            """
        )
        conn.execute(
            "INSERT INTO messages(id, source, lane) VALUES ('m1', 'dump', 'inbox')"
        )
        conn.execute(
            "INSERT INTO messages_fts(id, subject, body, from_addr) "
            "VALUES ('m1', 'Sub', 'Body here', 'a@b')"
        )
        conn.commit()
        rows = el.iter_candidates(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], el.embed_text("Sub", "Body here"))
        self.assertNotIn("Subject:", rows[0]["text"])


class OllamaPayloadTests(unittest.TestCase):
    def _capture(self, **kwargs):
        captured: dict = {}

        def opener(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))

            class Resp:
                status = 200
                code = 200

                def read(self):
                    return json.dumps({"embeddings": [[0.1] * 1024]}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Resp()

        el.ollama_embed_batch(["hello"], el.DEFAULT_MODEL, opener=opener, **kwargs)
        return captured["payload"]

    def test_default_has_dimensions_no_num_ctx(self):
        payload = self._capture()
        self.assertEqual(payload["model"], "qwen3-embedding:8b")
        self.assertEqual(payload["dimensions"], 1024)
        self.assertNotIn("options", payload)

    def test_quote_strip_sends_modest_num_ctx(self):
        payload = self._capture(num_ctx=4096)
        self.assertEqual(payload["dimensions"], 1024)
        self.assertEqual(payload["options"]["num_ctx"], 4096)


class LockTests(unittest.TestCase):
    def test_lock_per_batch_and_action_required_refuses(self):
        conn = incremental_conn()
        insert_msg(conn, "n1")
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "mailroom.write.lock"
            action = Path(tmp) / "ACTION_REQUIRED"
            el.backfill(
                conn,
                quote_strip=True,
                embed_fn=fake_embed,
                lock=True,
                lock_path=lock,
                action_required_path=action,
                log=lambda _m: None,
            )
            self.assertTrue(lock.is_file())
            action.write_text("stop\n", encoding="utf-8")
            insert_msg(conn, "n2")
            with self.assertRaises(el.EmbedError) as ctx:
                el.backfill(
                    conn,
                    quote_strip=True,
                    embed_fn=fake_embed,
                    lock=True,
                    lock_path=lock,
                    action_required_path=action,
                    log=lambda _m: None,
                )
            self.assertIn("action-required", str(ctx.exception))


class CliTests(unittest.TestCase):
    def test_quote_strip_default_off(self):
        args = eb.build_parser().parse_args([])
        self.assertFalse(args.quote_strip)
        self.assertFalse(args.lock)
        self.assertIsNone(args.num_ctx)

    def test_quote_strip_and_max_chars(self):
        args = eb.build_parser().parse_args(
            ["--quote-strip", "--max-chars", "3000", "--lock", "--num-ctx", "2048"]
        )
        self.assertTrue(args.quote_strip)
        self.assertEqual(args.max_chars, 3000)
        self.assertTrue(args.lock)
        self.assertEqual(args.num_ctx, 2048)


class HygieneTests(unittest.TestCase):
    def test_incremental_source_never_drops_vec0(self):
        for name in ("embed_backfill.py", "mail_clean.py", "thread_graph.py", "embed_document.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("DROP TABLE", text)
        lib = (SCRIPTS / "embed_lib.py").read_text(encoding="utf-8")
        # Rem-path dims-mismatch hint may mention DROP; it is not executed.
        # Incremental path never calls apply_schema / _assert_vec_dims.
        self.assertIn("refuse (no vec0 create/rebuild)", lib)
        self.assertIn("if quote_strip:", lib)
        self.assertEqual(lib.count("DROP TABLE IF EXISTS message_embeddings"), 1)
        self.assertIn("This is expected if you previously applied the OpenAI", lib)

    def test_no_secrets(self):
        for path in (
            SCRIPTS / "embed_lib.py",
            SCRIPTS / "embed_backfill.py",
            SCRIPTS / "mail_clean.py",
            SCRIPTS / "thread_graph.py",
            SCRIPTS / "embed_document.py",
        ):
            text = path.read_text(encoding="utf-8")
            for token in (
                "EXAMPLE_USER_LOCAL",
                "@example.invalid",
                "-----BEGIN",
                "ak_live",
            ):
                self.assertNotIn(token, text, msg=path.name)

    def test_daily_plist_not_a_rem_launchagent(self):
        plist = (ROOT / "launchd" / "com.mailroom.daily.plist").read_text(encoding="utf-8")
        self.assertNotIn("id-rem", plist)
        self.assertNotIn("min-chars", plist)
        self.assertNotIn("embed-rem", plist)


if __name__ == "__main__":
    unittest.main()
