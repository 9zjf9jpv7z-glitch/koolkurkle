#!/usr/bin/env python3
"""--max-chars / --min-chars filter + clean partition. No Ollama, no network."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_backfill  # noqa: E402
import embed_lib as el  # noqa: E402
from mailroom_test_util import fake_embed_fn, insert_message, open_mem  # noqa: E402

SPLIT = 1000


def insert_embed_len(conn, message_id: str, n: int, **kwargs) -> None:
    """Insert a message whose ``embed_text`` payload has length ``n`` (n >= 1)."""
    if n < 1:
        raise ValueError("n must be >= 1 so the FTS body is non-empty")
    prefix = "S\n\n"
    if n > len(prefix):
        subject = "S"
        body = "x" * (n - len(prefix))
    else:
        subject = ""
        body = "x" * n
    insert_message(conn, message_id, subject=subject, body=body, **kwargs)
    got = len(el.embed_text(subject, body))
    if got != n:
        raise AssertionError(f"embed_text length {got} != requested {n}")


class ValidateCharBoundsTests(unittest.TestCase):
    def test_omitted_and_single_flags(self):
        self.assertEqual(el.validate_char_bounds(None, None), (None, None))
        self.assertEqual(el.validate_char_bounds(1000, None), (1000, None))
        self.assertEqual(el.validate_char_bounds(None, 1000), (None, 1000))
        self.assertEqual(el.validate_char_bounds(2000, 1000), (2000, 1000))

    def test_both_require_max_greater_than_min(self):
        with self.assertRaises(el.EmbedError) as ctx:
            el.validate_char_bounds(1000, 1000)
        self.assertIn("max-chars must be > --min-chars", str(ctx.exception))
        with self.assertRaises(el.EmbedError):
            el.validate_char_bounds(999, 1000)
        with self.assertRaises(el.EmbedError):
            el.validate_char_bounds(-1, None)
        with self.assertRaises(el.EmbedError):
            el.validate_char_bounds(None, -5)

    def test_partition_predicate(self):
        self.assertTrue(el.in_char_bounds(1000, max_chars=1000))
        self.assertFalse(el.in_char_bounds(1001, max_chars=1000))
        self.assertFalse(el.in_char_bounds(1000, min_chars=1000))
        self.assertTrue(el.in_char_bounds(1001, min_chars=1000))
        self.assertIsNone(el.char_bound_skip(1000, max_chars=1000))
        self.assertEqual(el.char_bound_skip(1001, max_chars=1000), "too_long")
        self.assertEqual(el.char_bound_skip(1000, min_chars=1000), "too_short")
        self.assertIsNone(el.char_bound_skip(1001, min_chars=1000))
        self.assertTrue(el.in_char_bounds(50, None, None))


class CharBoundFilterTests(unittest.TestCase):
    def test_max_chars_keeps_eq_excludes_over(self):
        conn = open_mem()
        insert_embed_len(conn, "eq-1000", SPLIT)
        insert_embed_len(conn, "over-1001", SPLIT + 1)
        insert_embed_len(conn, "under-999", SPLIT - 1)
        counts = el.candidate_counts(conn, max_chars=SPLIT)
        self.assertEqual(counts["candidates"], 2)
        self.assertEqual(counts["skipped_too_long"], 1)
        self.assertEqual(counts["skipped_too_short"], 0)
        ids = {row["id"] for row in el.iter_candidates(conn, max_chars=SPLIT)}
        self.assertEqual(ids, {"eq-1000", "under-999"})
        self.assertTrue(all(len(row["text"]) <= SPLIT for row in el.iter_candidates(conn, max_chars=SPLIT)))
        conn.close()

    def test_min_chars_keeps_over_excludes_eq(self):
        conn = open_mem()
        insert_embed_len(conn, "eq-1000", SPLIT)
        insert_embed_len(conn, "over-1001", SPLIT + 1)
        insert_embed_len(conn, "under-999", SPLIT - 1)
        counts = el.candidate_counts(conn, min_chars=SPLIT)
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["skipped_too_short"], 2)
        self.assertEqual(counts["skipped_too_long"], 0)
        ids = {row["id"] for row in el.iter_candidates(conn, min_chars=SPLIT)}
        self.assertEqual(ids, {"over-1001"})
        self.assertTrue(all(len(row["text"]) > SPLIT for row in el.iter_candidates(conn, min_chars=SPLIT)))
        conn.close()

    def test_max_and_min_1000_never_double_embed(self):
        conn = open_mem()
        lengths = (1, 999, 1000, 1001, 5000)
        for n in lengths:
            insert_embed_len(conn, f"len-{n}", n)
        short_ids = {row["id"] for row in el.iter_candidates(conn, max_chars=SPLIT)}
        long_ids = {row["id"] for row in el.iter_candidates(conn, min_chars=SPLIT)}
        all_ids = {row["id"] for row in el.iter_candidates(conn)}
        self.assertEqual(short_ids & long_ids, set())
        self.assertEqual(short_ids | long_ids, all_ids)
        self.assertIn("len-1000", short_ids)
        self.assertNotIn("len-1000", long_ids)
        short_counts = el.candidate_counts(conn, max_chars=SPLIT)
        long_counts = el.candidate_counts(conn, min_chars=SPLIT)
        unfiltered = el.candidate_counts(conn)
        self.assertEqual(
            short_counts["candidates"] + long_counts["candidates"],
            unfiltered["candidates"],
        )
        self.assertEqual(short_counts["skipped_too_short"], 0)
        self.assertEqual(long_counts["skipped_too_long"], 0)
        # Each side's skips are the other side's candidates.
        self.assertEqual(short_counts["skipped_too_long"], long_counts["candidates"])
        self.assertEqual(long_counts["skipped_too_short"], short_counts["candidates"])
        conn.close()

    def test_length_uses_embed_text_after_char_cap(self):
        conn = open_mem()
        # Body far over CHAR_CAP; hashed / filtered length is the capped payload.
        insert_message(conn, "capped", subject="S", body="y" * (el.CHAR_CAP + 800))
        payload = el.embed_text("S", "y" * (el.CHAR_CAP + 800))
        self.assertEqual(len(payload), el.CHAR_CAP)
        over_cap = el.candidate_counts(conn, max_chars=el.CHAR_CAP)
        self.assertEqual(over_cap["candidates"], 1)
        self.assertEqual(over_cap["skipped_too_long"], 0)
        below_cap = el.candidate_counts(conn, max_chars=el.CHAR_CAP - 1)
        self.assertEqual(below_cap["candidates"], 0)
        self.assertEqual(below_cap["skipped_too_long"], 1)
        # min-chars at CHAR_CAP excludes the capped payload (len == CHAR_CAP, not >).
        min_at_cap = el.candidate_counts(conn, min_chars=el.CHAR_CAP)
        self.assertEqual(min_at_cap["candidates"], 0)
        self.assertEqual(min_at_cap["skipped_too_short"], 1)
        min_under = el.candidate_counts(conn, min_chars=el.CHAR_CAP - 1)
        self.assertEqual(min_under["candidates"], 1)
        conn.close()

    def test_backfill_respects_filter_and_logs_skips(self):
        conn = open_mem()
        insert_embed_len(conn, "short", 40)
        insert_embed_len(conn, "long", 2500)
        logs = []
        short_run = el.backfill(
            conn,
            embed_fn=fake_embed_fn(),
            max_chars=SPLIT,
            log=logs.append,
        )
        self.assertEqual(short_run["embedded"], 1)
        self.assertEqual(short_run["skipped_too_long"], 1)
        self.assertEqual(short_run["skipped_too_short"], 0)
        self.assertTrue(any("skipped_too_long=1" in line for line in logs))
        stored = {
            row[0] for row in conn.execute("SELECT message_id FROM embedding_meta")
        }
        self.assertEqual(stored, {"short"})
        long_logs = []
        long_run = el.backfill(
            conn,
            embed_fn=fake_embed_fn(),
            min_chars=SPLIT,
            log=long_logs.append,
        )
        self.assertEqual(long_run["embedded"], 1)
        # The already-embedded short row is not reclassified as too_short.
        self.assertEqual(long_run["skipped_already_embedded"], 1)
        self.assertEqual(long_run["skipped_too_short"], 0)
        self.assertTrue(any("skipped_too_short=0" in line for line in long_logs))
        stored = {
            row[0] for row in conn.execute("SELECT message_id FROM embedding_meta")
        }
        self.assertEqual(stored, {"short", "long"})
        conn.close()

    def test_already_embedded_not_counted_as_too_long(self):
        conn = open_mem()
        insert_embed_len(conn, "done-long", 2500)
        payload = el.embed_text("S", "x" * (2500 - 3))
        el.upsert_embedding(
            conn,
            message_id="done-long",
            vector=fake_embed_fn()([payload], el.DEFAULT_MODEL)[0],
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash=el.sha256_text(payload),
            char_count=len(payload),
        )
        insert_embed_len(conn, "new-long", 2500)
        counts = el.candidate_counts(conn, max_chars=SPLIT)
        self.assertEqual(counts["skipped_already_embedded"], 1)
        self.assertEqual(counts["skipped_too_long"], 1)
        self.assertEqual(counts["candidates"], 0)
        conn.close()

    def test_combines_with_id_shard(self):
        conn = open_mem()
        ids = [f"msg-{i:04d}" for i in range(40)]
        for i, message_id in enumerate(ids):
            insert_embed_len(conn, message_id, 100 if i % 2 == 0 else 2000)
        rem = 1
        short = {
            row["id"]
            for row in el.iter_candidates(conn, id_mod=2, id_rem=rem, max_chars=SPLIT)
        }
        long = {
            row["id"]
            for row in el.iter_candidates(conn, id_mod=2, id_rem=rem, min_chars=SPLIT)
        }
        shard = {row["id"] for row in el.iter_candidates(conn, id_mod=2, id_rem=rem)}
        self.assertEqual(short & long, set())
        self.assertEqual(short | long, shard)
        self.assertTrue(all(el.shard_remainder(mid, 2) == rem for mid in shard))
        short_counts = el.candidate_counts(conn, id_mod=2, id_rem=rem, max_chars=SPLIT)
        long_counts = el.candidate_counts(conn, id_mod=2, id_rem=rem, min_chars=SPLIT)
        shard_counts = el.candidate_counts(conn, id_mod=2, id_rem=rem)
        self.assertEqual(
            short_counts["candidates"] + long_counts["candidates"],
            shard_counts["candidates"],
        )
        conn.close()

    def test_omitted_flags_match_unfiltered_counts(self):
        conn = open_mem()
        insert_embed_len(conn, "a", 10)
        insert_embed_len(conn, "b", 2000)
        a = el.candidate_counts(conn)
        b = el.candidate_counts(conn, max_chars=None, min_chars=None)
        self.assertEqual(a, b)
        self.assertEqual(a["skipped_too_long"], 0)
        self.assertEqual(a["skipped_too_short"], 0)
        self.assertEqual(a["candidates"], 2)
        conn.close()


class CharBoundCliTests(unittest.TestCase):
    def test_parser_defaults_and_values(self):
        parser = embed_backfill.build_parser()
        args = parser.parse_args(["--db", "x.sqlite"])
        self.assertIsNone(args.max_chars)
        self.assertIsNone(args.min_chars)
        both = parser.parse_args(["--max-chars", "1000", "--min-chars", "100"])
        self.assertEqual(both.max_chars, 1000)
        self.assertEqual(both.min_chars, 100)

    def test_cli_rejects_equal_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = el.connect_db(db)
            el.ensure_mailroom_tables(conn)
            el.apply_schema(conn)
            insert_embed_len(conn, "cli-1", 40)
            conn.close()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "embed_backfill.py"),
                    "--db",
                    str(db),
                    "--dry-run",
                    "--max-chars",
                    "1000",
                    "--min-chars",
                    "1000",
                ],
                cwd=str(SCRIPTS),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("max-chars must be > --min-chars", proc.stderr)

    def test_cli_dry_run_logs_skips_and_works_with_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = el.connect_db(db)
            el.ensure_mailroom_tables(conn)
            el.apply_schema(conn)
            for i in range(12):
                insert_embed_len(conn, f"cli-{i:04d}", 40 if i % 2 == 0 else 2500)
            conn.close()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "embed_backfill.py"),
                    "--db",
                    str(db),
                    "--dry-run",
                    "--id-mod",
                    "2",
                    "--id-rem",
                    "1",
                    "--batch-size",
                    "1",
                    "--max-chars",
                    "1000",
                ],
                cwd=str(SCRIPTS),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("skipped_too_long", combined)
        self.assertIn("shard 1/2", combined)
        self.assertIn("char filter", combined)
        self.assertIn("max_chars=1000", combined)


if __name__ == "__main__":
    unittest.main()
