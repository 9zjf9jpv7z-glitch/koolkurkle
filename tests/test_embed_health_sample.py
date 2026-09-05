#!/usr/bin/env python3
"""embed_health_sample.sh follows live rem1/rem3/.latest, not finished embed_full."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "embed_health_sample.sh"


def _shell() -> str:
    return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


def run_health(logs: Path, *args: str, stale_sec: int = 90, run_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MAILARCHIVE_LOGS"] = str(logs)
    env["MAILARCHIVE_RUN"] = str(run_dir or (logs.parent / "run"))
    env["MAILARCHIVE_LAUNCHAGENTS"] = str(logs.parent / "missing-launchagents")
    env["EMBED_STALE_SEC"] = str(stale_sec)
    return subprocess.run(
        [_shell(), str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def write_log(path: Path, committed: int, total: int, age_sec: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"2026-09-05 10:00:00 starting\ncommitted {committed}/{total}\n",
        encoding="utf-8",
    )
    past = time.time() - age_sec
    os.utime(path, (past, past))


class EmbedHealthSampleTests(unittest.TestCase):
    def test_prefers_rem1_latest_over_finished_embed_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            write_log(logs / "embed_full_20260904_110702_numctx8192.log", 23856, 23856, age_sec=3600)
            live = logs / "embed_shard_rem1_20260905_120000.log"
            write_log(live, 120, 5000, age_sec=5)
            latest = logs / "embed_shard_rem1.latest"
            latest.symlink_to(live.name)
            proc = run_health(logs, "--pick-only", stale_sec=90)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            picked = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertTrue(picked, proc.stdout)
            self.assertTrue(
                any("rem1" in Path(p).name for p in picked),
                picked,
            )
            self.assertFalse(any("embed_full" in Path(p).name for p in picked), picked)

            js = run_health(logs, "--json", stale_sec=90)
            self.assertEqual(js.returncode, 0, js.stderr)
            data = json.loads(js.stdout.strip().splitlines()[-1])
            self.assertFalse(data["soft_stall"])
            kinds = {job["kind"] for job in data["jobs"]}
            self.assertIn("rem1", kinds)
            self.assertTrue(data["ignored_embed_full"])

    def test_finished_embed_full_alone_is_not_soft_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            write_log(logs / "embed_full_20260904_110702.log", 100, 100, age_sec=7200)
            proc = run_health(logs, "--json", stale_sec=90)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(data["soft_stall"])
            self.assertIn(data["status"], ("idle", "done"))

    def test_live_rem3_latest_is_sampled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            write_log(logs / "embed_full_old.log", 50, 50, age_sec=8000)
            rem3 = logs / "embed_shard_rem3_20260905.log"
            write_log(rem3, 10, 4000, age_sec=3)
            (logs / "embed_shard_rem3.latest").symlink_to(rem3.name)
            proc = run_health(logs, "--pick-only")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            picked = proc.stdout.strip()
            self.assertIn("rem3", picked)
            self.assertNotIn("embed_full", picked)

    def test_quiet_live_shard_is_soft_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            rem1 = logs / "embed_shard_rem1_stuck.log"
            write_log(rem1, 40, 9000, age_sec=400)
            (logs / "embed_shard_rem1.latest").symlink_to(rem1.name)
            proc = run_health(logs, "--json", stale_sec=90)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["soft_stall"])
            self.assertEqual(data["status"], "soft-stall")


if __name__ == "__main__":
    unittest.main()
