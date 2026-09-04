#!/usr/bin/env python3
"""Backfill candidate selection + mocked embeddings. No network."""

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
    fake_embed_fn,
    insert_message,
    one_hot,
    open_mem,
)


class KeychainTests(unittest.TestCase):
    def test_env_override_used_for_tests(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-not-real"}):
            key = el.read_openai_api_key()
        self.assertEqual(key, "sk-test-not-real")

    def test_keychain_tries_documented_names_w_last(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "not found")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(el.EmbedError) as ctx:
                el.read_openai_api_key(allow_env=True, run=fake_run)
        self.assertTrue(calls)
        self.assertEqual(calls[0][-1], "-w")
        self.assertEqual(calls[0][3], "openai-api-key")
        self.assertEqual(calls[0][5], "koolkurkle")
        msg = str(ctx.exception)
        self.assertIn("openai-api-key", msg)
        self.assertIn("koolkurkle", msg)
        self.assertNotIn("sk-", msg)

    def test_embed_batch_parses_fake_response(self):
        secret = "sk-must-not-appear"

        class Resp:
            def read(self):
                return json.dumps(
                    {
                        "data": [
                            {"index": 1, "embedding": [0.0, 1.0]},
                            {"index": 0, "embedding": [1.0, 0.0]},
                        ]
                    }
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=0):
            self.assertIn("Authorization", request.headers)
            return Resp()

        vecs = el.openai_embed_batch(["a", "b"], el.DEFAULT_MODEL, secret, opener=opener)
        self.assertEqual(vecs, [[1.0, 0.0], [0.0, 1.0]])

    def test_key_never_logged_on_http_error(self):
        secret = "sk-super-secret-do-not-print"

        def boom(request, timeout=0):
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/embeddings",
                401,
                "Unauthorized",
                {"Authorization": f"Bearer {secret}"},
                io.BytesIO(b'{"error":"nope"}'),
            )

        with self.assertRaises(el.EmbedError) as ctx:
            el.openai_embed_batch(["hello"], el.DEFAULT_MODEL, secret, opener=boom)
        text = str(ctx.exception)
        self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)


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
                env={**os.environ, "OPENAI_API_KEY": "sk-should-not-be-used-or-printed"},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("skipped_auth=1", combined)
        self.assertIn("candidate", combined)
        self.assertNotIn("sk-should-not-be-used-or-printed", combined)

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


class ParserTests(unittest.TestCase):
    def test_skip_auth_default_on(self):
        args = embed_backfill.build_parser().parse_args(["--db", "x.sqlite"])
        self.assertTrue(args.skip_auth)
        args = embed_backfill.build_parser().parse_args(["--no-skip-auth"])
        self.assertFalse(args.skip_auth)


if __name__ == "__main__":
    unittest.main()
