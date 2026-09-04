#!/usr/bin/env python3
"""Backfill candidate selection + mocked Ollama HTTP. No network."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_backfill  # noqa: E402
import embed_lib as el  # noqa: E402
from mailroom_test_util import (  # noqa: E402
    FakeHTTPResponse,
    fake_embed_fn,
    insert_message,
    one_hot,
    open_mem,
)


class NoOpenAITests(unittest.TestCase):
    def test_openai_keychain_path_removed(self):
        self.assertFalse(hasattr(el, "read_openai_api_key"))
        self.assertFalse(hasattr(el, "openai_embed_batch"))
        self.assertFalse(hasattr(el, "OPENAI_EMBEDDINGS_URL"))
        self.assertFalse(hasattr(el, "KEYCHAIN_CANDIDATES"))
        source = Path(el.__file__).read_text(encoding="utf-8")
        self.assertNotIn("OPENAI_API_KEY", source)
        self.assertNotIn("https://api.openai.com", source)
        self.assertEqual(el.DEFAULT_MODEL, "qwen3-embedding:8b")
        self.assertEqual(el.DEFAULT_OLLAMA_URL, "http://127.0.0.1:11434")


class OllamaHTTPTests(unittest.TestCase):
    def test_api_embed_parses_batch(self):
        urls = []

        def opener(request, timeout=0):
            urls.append(request.full_url)
            payload = json.loads(request.data.decode())
            self.assertEqual(payload["model"], "qwen3-embedding:8b")
            self.assertEqual(payload["input"], ["a", "b"])
            self.assertEqual(payload["dimensions"], el.DEFAULT_DIMS)
            native = [0.0] * el.NATIVE_DIMS
            other = [0.0] * el.NATIVE_DIMS
            native[0] = 1.0
            other[1] = 1.0
            return FakeHTTPResponse({"embeddings": [native, other]})

        vecs = el.ollama_embed_batch(
            ["a", "b"], el.DEFAULT_MODEL, opener=opener, dims=el.DEFAULT_DIMS
        )
        self.assertEqual(len(vecs), 2)
        self.assertEqual(len(vecs[0]), el.DEFAULT_DIMS)
        self.assertAlmostEqual(vecs[0][0], 1.0)
        self.assertAlmostEqual(vecs[1][1], 1.0)
        self.assertTrue(urls[0].endswith("/api/embed"))

    def test_v1_embeddings_fallback_on_404(self):
        urls = []

        def opener(request, timeout=0):
            urls.append(request.full_url)
            if request.full_url.endswith("/api/embed"):
                raise urllib.error.HTTPError(
                    request.full_url,
                    404,
                    "Not Found",
                    {},
                    io.BytesIO(b'{"error":"not found"}'),
                )
            return FakeHTTPResponse(
                {
                    "data": [
                        {"index": 1, "embedding": one_hot(1)},
                        {"index": 0, "embedding": one_hot(0)},
                    ]
                }
            )

        vecs = el.ollama_embed_batch(["a", "b"], el.DEFAULT_MODEL, opener=opener)
        self.assertEqual(vecs[0][0], 1.0)
        self.assertEqual(vecs[1][1], 1.0)
        self.assertTrue(any(u.endswith("/api/embed") for u in urls))
        self.assertTrue(any(u.endswith("/v1/embeddings") for u in urls))

    def test_matryoshka_truncate_renormalizes(self):
        long_vec = [3.0, 4.0] + [0.0] * (el.NATIVE_DIMS - 2)
        out = el.adapt_dims(long_vec, 2)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0], 0.6)
        self.assertAlmostEqual(out[1], 0.8)

    def test_zero_embedding_rejected(self):
        with self.assertRaises(el.EmbedError) as ctx:
            el.l2_normalize([0.0, 0.0])
        self.assertIn("zero embedding", str(ctx.exception))

    def test_connection_error_names_ollama_not_openai(self):
        def boom(request, timeout=0):
            raise urllib.error.URLError("connection refused")

        with self.assertRaises(el.EmbedError) as ctx:
            el.ollama_embed_batch(["hello"], el.DEFAULT_MODEL, opener=boom)
        text = str(ctx.exception)
        self.assertIn("Ollama", text)
        self.assertIn("qwen3-embedding:8b", text)
        self.assertNotIn("OpenAI", text)
        self.assertNotIn("api.openai.com", text)

    def test_http_error_has_no_auth_header(self):
        def boom(request, timeout=0):
            self.assertNotIn("Authorization", request.headers)
            raise urllib.error.HTTPError(
                "http://127.0.0.1:11434/api/embed",
                500,
                "nope",
                {},
                io.BytesIO(b'{"error":"nope"}'),
            )

        with self.assertRaises(el.EmbedError) as ctx:
            el.ollama_embed_batch(["hello"], el.DEFAULT_MODEL, opener=boom)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_model_id_aliases(self):
        self.assertEqual(el.model_id("qwen3-embedding:8b"), "qwen3-embedding-8b")
        self.assertEqual(el.model_id("qwen3-embedding"), "qwen3-embedding-8b")
        self.assertEqual(el.model_id("qwen3-embedding:latest"), "qwen3-embedding-8b")
        self.assertEqual(el.ollama_model_tag("qwen3-embedding-8b"), "qwen3-embedding:8b")


class CandidateTests(unittest.TestCase):
    def test_skips_auth_empty_and_already_embedded(self):
        conn = open_mem()
        insert_message(conn, "keep-dump", body="invoice 42", source="dump", lane="inbox")
        insert_message(
            conn, "keep-live", body="live body", source="imap-live", lane="inbox"
        )
        insert_message(conn, "auth-otp", body="your code is 123456", lane="auth")
        insert_message(conn, "empty-1", body="   ", lane="inbox")
        insert_message(conn, "done-1", body="already in", lane="inbox")
        payload = el.embed_text("Hello", "already in")
        el.upsert_embedding(
            conn,
            message_id="done-1",
            vector=one_hot(3),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash=el.sha256_text(payload),
            char_count=len(payload),
        )
        conn.commit()
        counts = el.candidate_counts(conn)
        self.assertEqual(counts["skipped_auth"], 1)
        self.assertEqual(counts["skipped_empty_body"], 1)
        self.assertEqual(counts["skipped_already_embedded"], 1)
        self.assertEqual(counts["reembed_hash_changed"], 0)
        self.assertEqual(counts["candidates"], 2)
        ids = {row["id"] for row in el.iter_candidates(conn)}
        self.assertEqual(ids, {"keep-dump", "keep-live"})
        stored = conn.execute("SELECT model, dims FROM embedding_meta").fetchone()
        self.assertEqual(stored["model"], "qwen3-embedding-8b")
        self.assertEqual(stored["dims"], el.DEFAULT_DIMS)
        conn.close()

    def test_hash_change_reembeds(self):
        conn = open_mem()
        insert_message(conn, "chg-1", subject="Sub", body="old body")
        el.upsert_embedding(
            conn,
            message_id="chg-1",
            vector=one_hot(4),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash="deadbeef",
            char_count=3,
        )
        conn.commit()
        counts = el.candidate_counts(conn)
        self.assertEqual(counts["reembed_hash_changed"], 1)
        self.assertEqual(counts["candidates"], 1)
        rows = el.iter_candidates(conn)
        self.assertTrue(rows[0]["reembed"])
        conn.close()

    def test_dry_run_does_not_call_embed(self):
        conn = open_mem()
        insert_message(conn, "d-1", body="please embed me")
        called = {"n": 0}

        def boom(texts, model):
            called["n"] += 1
            raise AssertionError("dry-run must not embed")

        logs = []
        counts = el.backfill(conn, dry_run=True, embed_fn=boom, log=logs.append)
        self.assertEqual(called["n"], 0)
        self.assertEqual(counts["would_embed"], 1)
        self.assertEqual(counts["embedded"], 0)
        self.assertTrue(any("dry-run" in line for line in logs))
        self.assertTrue(any("qwen3-embedding-8b" in line for line in logs))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0], 0
        )
        conn.close()

    def test_backfill_writes_and_is_resume_safe(self):
        conn = open_mem()
        insert_message(conn, "b-1", subject="S", body="first")
        insert_message(conn, "b-2", subject="S", body="second")
        logs = []
        counts = el.backfill(
            conn,
            embed_fn=fake_embed_fn(),
            batch_size=1,
            log=logs.append,
        )
        self.assertEqual(counts["embedded"], 2)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0], 2
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0], 2
        )
        again = el.backfill(conn, embed_fn=fake_embed_fn(), log=logs.append)
        self.assertEqual(again["embedded"], 0)
        self.assertEqual(again["skipped_already_embedded"], 2)
        conn.close()

    def test_truncate_subject_body(self):
        long_body = "x" * (el.CHAR_CAP + 500)
        text = el.embed_text("subj", long_body)
        self.assertEqual(len(text), el.CHAR_CAP)
        self.assertTrue(text.startswith("subj\n\nxxx"))

    def test_query_text_adds_instruct_prefix(self):
        q = el.query_text("  receipt from apple ")
        self.assertTrue(q.startswith("Instruct:"))
        self.assertTrue(q.endswith("receipt from apple"))
        self.assertNotIn("Instruct:", el.embed_text("S", "body"))

    def test_cli_dry_run_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = el.connect_db(db)
            el.ensure_mailroom_tables(conn)
            el.apply_schema(conn)
            insert_message(conn, "cli-1", body="candidate body", lane="inbox")
            insert_message(conn, "cli-auth", body="2fa code", lane="auth")
            conn.close()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "embed_backfill.py"),
                    "--db",
                    str(db),
                    "--dry-run",
                ],
                cwd=str(SCRIPTS),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("skipped_auth=1", combined)
        self.assertIn("candidate", combined)
        self.assertIn("dry-run", combined)
        self.assertNotIn("OpenAI", combined)
        self.assertNotIn("api.openai.com", combined)

    def test_cli_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "embed_backfill.py"), "--help"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("--skip-auth", proc.stdout)
        self.assertIn("--batch-size", proc.stdout)
        self.assertIn("--model", proc.stdout)
        self.assertIn("--ollama-url", proc.stdout)
        self.assertIn("qwen3-embedding:8b", proc.stdout)
        self.assertIn("No OpenAI", proc.stdout)
        self.assertNotIn("text-embedding-3-small", proc.stdout)


class ParserTests(unittest.TestCase):
    def test_skip_auth_default_on(self):
        args = embed_backfill.build_parser().parse_args(["--db", "x.sqlite"])
        self.assertTrue(args.skip_auth)
        self.assertEqual(args.ollama_url, el.DEFAULT_OLLAMA_URL)
        self.assertEqual(args.model, "qwen3-embedding:8b")
        self.assertEqual(args.dims, el.DEFAULT_DIMS)
        args = embed_backfill.build_parser().parse_args(["--no-skip-auth"])
        self.assertFalse(args.skip_auth)


if __name__ == "__main__":
    unittest.main()
