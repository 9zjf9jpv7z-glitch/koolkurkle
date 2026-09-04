#!/usr/bin/env python3
"""Id-hash sharding + missing-only shard merge. No Ollama, no network."""

from __future__ import annotations

import sqlite3
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
from mailroom_test_util import (  # noqa: E402
    fake_embed_fn,
    insert_message,
    one_hot,
    open_mem,
)

# Mix of UUID, numeric-looking, and plain text ids — hash, do not int().
SAMPLE_IDS = [
    "550e8400-e29b-41d4-a716-446655440000",
    "not-a-number",
    "42",
    "00000000-0000-0000-0000-000000000001",
    *[f"msg-{i:04d}" for i in range(80)],
]


def _seed_candidates(conn: sqlite3.Connection, ids: list[str]) -> None:
    for i, message_id in enumerate(ids):
        insert_message(
            conn,
            message_id,
            subject=f"S{i}",
            body=f"body for {message_id}",
            lane="inbox",
        )


class ShardHashTests(unittest.TestCase):
    def test_hash_is_stable_and_not_int_parse(self):
        a = el.message_id_shard_hash("42")
        b = el.message_id_shard_hash("42")
        self.assertEqual(a, b)
        self.assertIsInstance(a, int)
        self.assertGreaterEqual(a, 0)
        # Numeric-looking ids must hash the utf-8 string, not int(id).
        self.assertNotEqual(el.shard_remainder("42", 100), 42 % 100)
        self.assertTrue(el.in_id_shard("any", None, None))

    def test_validate_shard_pairs(self):
        self.assertEqual(el.validate_shard(None, None), (None, None))
        self.assertEqual(el.validate_shard(2, 1), (2, 1))
        with self.assertRaises(el.EmbedError):
            el.validate_shard(2, None)
        with self.assertRaises(el.EmbedError):
            el.validate_shard(None, 0)
        with self.assertRaises(el.EmbedError):
            el.validate_shard(1, 0)
        with self.assertRaises(el.EmbedError):
            el.validate_shard(2, 2)
        with self.assertRaises(el.EmbedError):
            el.validate_shard(2, -1)


class ShardPartitionTests(unittest.TestCase):
    def _assert_partition(self, ids: list[str], mod: int) -> None:
        buckets = {rem: set() for rem in range(mod)}
        for message_id in ids:
            rem = el.shard_remainder(message_id, mod)
            self.assertGreaterEqual(rem, 0)
            self.assertLess(rem, mod)
            buckets[rem].add(message_id)
        for left in range(mod):
            for right in range(left + 1, mod):
                self.assertEqual(buckets[left] & buckets[right], set())
        self.assertEqual(set().union(*buckets.values()), set(ids))

    def test_hash_mod_2_and_3_cover_and_disjoint(self):
        self._assert_partition(SAMPLE_IDS, 2)
        self._assert_partition(SAMPLE_IDS, 3)

    def test_iter_candidates_shards_are_disjoint_and_cover(self):
        conn = open_mem()
        _seed_candidates(conn, SAMPLE_IDS)
        all_ids = {row["id"] for row in el.iter_candidates(conn)}
        self.assertEqual(all_ids, set(SAMPLE_IDS))

        for mod in (2, 3):
            parts = []
            for rem in range(mod):
                ids = {row["id"] for row in el.iter_candidates(conn, id_mod=mod, id_rem=rem)}
                parts.append(ids)
                expected = {mid for mid in SAMPLE_IDS if el.shard_remainder(mid, mod) == rem}
                self.assertEqual(ids, expected)
            for i in range(mod):
                for j in range(i + 1, mod):
                    self.assertEqual(parts[i] & parts[j], set())
            self.assertEqual(set().union(*parts), all_ids)

            unsharded = el.candidate_counts(conn)
            summed = {
                "joined": 0,
                "candidates": 0,
                "skipped_auth": 0,
                "skipped_empty_body": 0,
                "skipped_already_embedded": 0,
                "reembed_hash_changed": 0,
            }
            for rem in range(mod):
                part = el.candidate_counts(conn, id_mod=mod, id_rem=rem)
                self.assertEqual(part["id_mod"], mod)
                self.assertEqual(part["id_rem"], rem)
                for key in summed:
                    summed[key] += part[key]
            for key in summed:
                self.assertEqual(summed[key], unsharded[key], key)
        conn.close()

    def test_omitted_flags_match_unsharded_counts(self):
        conn = open_mem()
        _seed_candidates(conn, SAMPLE_IDS[:10])
        insert_message(conn, "auth-x", body="otp", lane="auth")
        a = el.candidate_counts(conn)
        b = el.candidate_counts(conn, id_mod=None, id_rem=None)
        self.assertEqual(a, b)
        self.assertNotIn("id_mod", a)
        self.assertEqual(
            {row["id"] for row in el.iter_candidates(conn)},
            {row["id"] for row in el.iter_candidates(conn, id_mod=None, id_rem=None)},
        )
        conn.close()

    def test_backfill_respects_shard_and_dry_run_logs(self):
        conn = open_mem()
        _seed_candidates(conn, SAMPLE_IDS[:20])
        rem0 = {mid for mid in SAMPLE_IDS[:20] if el.shard_remainder(mid, 2) == 0}
        logs = []
        counts = el.backfill(
            conn,
            embed_fn=fake_embed_fn(),
            id_mod=2,
            id_rem=0,
            log=logs.append,
        )
        self.assertEqual(counts["embedded"], len(rem0))
        stored = {
            row[0]
            for row in conn.execute("SELECT message_id FROM embedding_meta")
        }
        self.assertEqual(stored, rem0)
        self.assertTrue(any("shard 0/2" in line for line in logs))
        again = el.backfill(
            conn, embed_fn=fake_embed_fn(), id_mod=2, id_rem=0, log=logs.append
        )
        self.assertEqual(again["embedded"], 0)
        dry_logs = []
        dry = el.backfill(
            conn,
            dry_run=True,
            embed_fn=fake_embed_fn(),
            id_mod=2,
            id_rem=1,
            log=dry_logs.append,
        )
        rem1 = {mid for mid in SAMPLE_IDS[:20] if el.shard_remainder(mid, 2) == 1}
        self.assertEqual(dry["would_embed"], len(rem1))
        self.assertEqual(dry["embedded"], 0)
        self.assertTrue(any("shard 1/2" in line for line in dry_logs))
        conn.close()


class MergeShardsTests(unittest.TestCase):
    def _embed(self, conn: sqlite3.Connection, message_id: str, index: int) -> None:
        payload = el.embed_text("Hello", f"body for {message_id}")
        el.upsert_embedding(
            conn,
            message_id=message_id,
            vector=one_hot(index),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash=el.sha256_text(payload),
            char_count=len(payload),
        )

    def test_merge_inserts_only_missing_and_is_idempotent(self):
        primary = open_mem()
        secondary = open_mem()
        ids = ["keep-primary", "shared", "only-secondary"]
        for conn in (primary, secondary):
            _seed_candidates(conn, ids)
        self._embed(primary, "keep-primary", 0)
        self._embed(primary, "shared", 1)
        primary_hash = primary.execute(
            "SELECT text_hash FROM embedding_meta WHERE message_id='shared'"
        ).fetchone()[0]
        # Secondary also has shared (different hash) plus a new row.
        self._embed(secondary, "shared", 5)
        self._embed(secondary, "only-secondary", 2)
        secondary.commit()
        primary.commit()

        logs = []
        first = el.merge_shards(primary, secondary, log=logs.append)
        self.assertEqual(first["examined"], 2)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["skipped_already_present"], 1)
        self.assertEqual(first["missing_vector"], 0)
        self.assertEqual(first["errors"], 0)
        self.assertEqual(
            primary.execute(
                "SELECT COUNT(*) FROM embedding_meta"
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            primary.execute(
                "SELECT COUNT(*) FROM message_embeddings"
            ).fetchone()[0],
            3,
        )
        still = primary.execute(
            "SELECT text_hash FROM embedding_meta WHERE message_id='shared'"
        ).fetchone()[0]
        self.assertEqual(still, primary_hash)
        self.assertIsNotNone(
            primary.execute(
                "SELECT 1 FROM embedding_meta WHERE message_id='only-secondary'"
            ).fetchone()
        )

        second = el.merge_shards(primary, secondary, log=logs.append)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["skipped_already_present"], 2)
        self.assertEqual(second["examined"], 2)
        self.assertEqual(
            primary.execute(
                "SELECT COUNT(*) FROM embedding_meta"
            ).fetchone()[0],
            3,
        )
        primary.close()
        secondary.close()

    def test_merge_dry_run_does_not_write(self):
        primary = open_mem()
        secondary = open_mem()
        insert_message(primary, "n-1", body="body for n-1")
        insert_message(secondary, "n-1", body="body for n-1")
        self._embed(secondary, "n-1", 3)
        dry = el.merge_shards(primary, secondary, dry_run=True, log=lambda _m: None)
        self.assertEqual(dry["inserted"], 1)
        self.assertEqual(
            primary.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0],
            0,
        )
        primary.close()
        secondary.close()

    def test_merge_missing_vector_counted(self):
        primary = open_mem()
        secondary = open_mem()
        insert_message(primary, "ghost", body="body for ghost")
        insert_message(secondary, "ghost", body="body for ghost")
        payload = el.embed_text("Hello", "body for ghost")
        secondary.execute(
            """
            INSERT INTO embedding_meta(
              message_id, model, model_version, created_at, text_hash, char_count, dims
            ) VALUES (?, ?, 'v1', '2026-01-01T00:00:00+00:00', ?, ?, ?)
            """,
            (
                "ghost",
                el.DEFAULT_MODEL_ID,
                el.sha256_text(payload),
                len(payload),
                el.DEFAULT_DIMS,
            ),
        )
        secondary.commit()
        counts = el.merge_shards(primary, secondary, log=lambda _m: None)
        self.assertEqual(counts["missing_vector"], 1)
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(
            primary.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0],
            0,
        )
        primary.close()
        secondary.close()

    def test_merge_dims_mismatch_rejected(self):
        primary = open_mem()
        secondary = sqlite3.connect(":memory:")
        secondary.row_factory = sqlite3.Row
        el.load_sqlite_vec(secondary)
        el.ensure_mailroom_tables(secondary)
        el.apply_schema(secondary, dims=256)
        insert_message(secondary, "d-1", body="body for d-1")
        payload = el.embed_text("Hello", "body for d-1")
        el.upsert_embedding(
            secondary,
            message_id="d-1",
            vector=one_hot(0, dims=256),
            model=el.DEFAULT_MODEL,
            model_version="v1",
            text_hash=el.sha256_text(payload),
            char_count=len(payload),
            dims=256,
        )
        with self.assertRaises(el.EmbedError) as ctx:
            el.merge_shards(primary, secondary)
        self.assertIn("dims mismatch", str(ctx.exception))
        primary.close()
        secondary.close()


class ShardCliTests(unittest.TestCase):
    def test_parser_requires_both_flags(self):
        parser = embed_backfill.build_parser()
        args = parser.parse_args(["--db", "x.sqlite"])
        self.assertIsNone(args.id_mod)
        self.assertIsNone(args.id_rem)
        both = parser.parse_args(["--id-mod", "2", "--id-rem", "1"])
        self.assertEqual(both.id_mod, 2)
        self.assertEqual(both.id_rem, 1)

    def test_cli_help_lists_shard_flags(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "embed_backfill.py"), "--help"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--id-mod", proc.stdout)
        self.assertIn("--id-rem", proc.stdout)

    def test_cli_errors_when_only_one_shard_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = el.connect_db(db)
            el.ensure_mailroom_tables(conn)
            el.apply_schema(conn)
            insert_message(conn, "cli-1", body="candidate body")
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
                ],
                cwd=str(SCRIPTS),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("required together", proc.stderr)

    def test_cli_dry_run_logs_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            conn = el.connect_db(db)
            el.ensure_mailroom_tables(conn)
            el.apply_schema(conn)
            _seed_candidates(conn, SAMPLE_IDS[:12])
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
                    "0",
                ],
                cwd=str(SCRIPTS),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("shard 0/2", combined)
        self.assertIn("candidates in shard", combined)

    def test_merge_cli_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary.sqlite"
            secondary = Path(tmp) / "secondary.sqlite"
            pconn = el.connect_db(primary)
            sconn = el.connect_db(secondary)
            el.ensure_mailroom_tables(pconn)
            el.ensure_mailroom_tables(sconn)
            el.apply_schema(pconn)
            el.apply_schema(sconn)
            insert_message(pconn, "m-new", body="body for m-new")
            insert_message(sconn, "m-new", body="body for m-new")
            payload = el.embed_text("Hello", "body for m-new")
            el.upsert_embedding(
                sconn,
                message_id="m-new",
                vector=one_hot(7),
                model=el.DEFAULT_MODEL,
                model_version="v1",
                text_hash=el.sha256_text(payload),
                char_count=len(payload),
            )
            pconn.commit()
            sconn.commit()
            pconn.close()
            sconn.close()
            cmd = [
                sys.executable,
                str(SCRIPTS / "embed_merge_shards.py"),
                "--primary-db",
                str(primary),
                "--secondary-db",
                str(secondary),
            ]
            first = subprocess.run(cmd, cwd=str(SCRIPTS), capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("inserted=1", first.stdout)
            second = subprocess.run(cmd, cwd=str(SCRIPTS), capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("inserted=0", second.stdout)
            self.assertIn("skipped_already_present=1", second.stdout)

    def test_merge_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "embed_merge_shards.py"), "--help"],
            cwd=str(SCRIPTS),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--primary-db", proc.stdout)
        self.assertIn("--secondary-db", proc.stdout)
        self.assertIn("Missing-only", proc.stdout)


if __name__ == "__main__":
    unittest.main()
