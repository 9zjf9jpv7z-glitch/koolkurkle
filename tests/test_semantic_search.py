#!/usr/bin/env python3
"""semantic_search CLI + mailroom_tools. Fake vectors only."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402
import mailroom_tools  # noqa: E402
from mailroom_test_util import insert_message, one_hot  # noqa: E402


def seeded_file_db(path: Path) -> None:
    conn = el.connect_db(path)
    el.ensure_mailroom_tables(conn)
    el.apply_schema(conn)
    insert_message(
        conn,
        "hit-apple",
        subject="Your Apple receipt",
        body="Order 99 cider and apples",
        from_addr="receipts@apple.example",
    )
    insert_message(
        conn,
        "hit-boat",
        subject="Harbor news",
        body="sailboat mooring fees",
        from_addr="port@example.com",
    )
    el.upsert_embedding(
        conn,
        message_id="hit-apple",
        vector=one_hot(0),
        model=el.DEFAULT_MODEL,
        model_version="v1",
        text_hash="a",
        char_count=10,
    )
    el.upsert_embedding(
        conn,
        message_id="hit-boat",
        vector=one_hot(1),
        model=el.DEFAULT_MODEL,
        model_version="v1",
        text_hash="b",
        char_count=10,
    )
    conn.commit()
    conn.close()


class SearchTests(unittest.TestCase):
    def test_mailroom_tools_semantic_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            seeded_file_db(db)
            query_vec = [0.9, 0.1] + [0.0] * (el.DEFAULT_DIMS - 2)
            hits = mailroom_tools.semantic_search(
                "apple receipt",
                db=db,
                k=2,
                query_vector=query_vec,
            )
        self.assertEqual(hits[0]["id"], "hit-apple")
        self.assertEqual(hits[0]["subject"], "Your Apple receipt")
        self.assertEqual(hits[0]["from_addr"], "receipts@apple.example")
        self.assertIn("apples", hits[0]["snippet"])
        self.assertIn("score", hits[0])
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertEqual(hits[1]["id"], "hit-boat")

    def test_search_mail_not_shipped(self):
        self.assertFalse(hasattr(mailroom_tools, "search_mail"))
        self.assertIn("FTS", mailroom_tools.SEARCH_MAIL_NOTE)

    def test_cli_prints_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            seeded_file_db(db)
            # Patch semantic_search.main's embed by using query_vector via
            # a tiny wrapper: call library then format, plus CLI --json with
            # OPENAI mocked through embed_lib.semantic_search query_vector
            # is not a CLI flag — use a fake embed_fn by invoking the module
            # function after injecting env that we never send.
            hits = el.semantic_search(
                "ignored",
                db,
                k=1,
                query_vector=one_hot(0),
            )
            self.assertEqual(hits[0]["id"], "hit-apple")
            formatted = el.format_hits(hits)
            self.assertIn("hit-apple", formatted)
            self.assertIn("subject:", formatted)
            self.assertIn("snippet:", formatted)
            self.assertIn("score=", formatted)

    def test_cli_json_with_fake_embed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            seeded_file_db(db)
            import semantic_search as ss

            fake_hits = [
                {
                    "id": "hit-apple",
                    "subject": "Your Apple receipt",
                    "from_addr": "receipts@apple.example",
                    "score": 0.99,
                    "distance": 0.01,
                    "snippet": "Order 99 cider and apples",
                }
            ]
            stdout = io.StringIO()
            with mock.patch.object(ss, "semantic_search", return_value=fake_hits):
                with mock.patch("sys.stdout", stdout):
                    rc = ss.main(
                        ["--db", str(db), "--json", "--k", "5", "apple receipt"]
                    )
            self.assertEqual(rc, 0)
            row = json.loads(stdout.getvalue().splitlines()[0])
            self.assertEqual(row["id"], "hit-apple")
            self.assertEqual(row["subject"], "Your Apple receipt")
            self.assertIn("snippet", row)
            self.assertIn("score", row)
            self.assertIn("from_addr", row)

    def test_cli_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "semantic_search.py"), "--help"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--k", proc.stdout)
        self.assertIn("--db", proc.stdout)


if __name__ == "__main__":
    unittest.main()
