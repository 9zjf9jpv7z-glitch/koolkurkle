"""CLI tests for ask_mail probes E/F. No live models."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from unittest.mock import patch

import ask_mail_generate_probes as ask_mail
import mailroom_generate as mg


class ProbeFTests(unittest.TestCase):
    def test_fail_open_labeled(self) -> None:
        hits_path = Path(tempfile.gettempdir()) / "mailroom-hits.json"
        hits_path.write_text(json.dumps({"hits": [{"id": "m1", "score": 0.9}]}), encoding="utf-8")

        class NS:
            query = "q"
            hits_json = str(hits_path)
            retrieve_module = None
            rerank_module = None
            embed_model = None

        with patch.object(mg, "unload_embed"), patch.object(
            mg, "generate_from_hits", side_effect=mg.GenerateDown("down")
        ):
            rc = ask_mail.cmd_probe_f(NS())
        self.assertEqual(rc, 0)

    def test_live_success_not_a_pass_for_f(self) -> None:
        hits_path = Path(tempfile.gettempdir()) / "mailroom-hits.json"
        hits_path.write_text(json.dumps(["h"]), encoding="utf-8")

        class NS:
            query = "q"
            hits_json = str(hits_path)
            retrieve_module = None
            rerank_module = None
            embed_model = None

        with patch.object(mg, "unload_embed"), patch.object(
            mg, "generate_from_hits", return_value="pong"
        ):
            rc = ask_mail.cmd_probe_f(NS())
        self.assertEqual(rc, 1)


class JitOffTests(unittest.TestCase):
    def test_set_existing_key_only(self) -> None:
        data = {"server": {"justInTimeModelLoading": True, "port": 1234}}
        self.assertTrue(ask_mail._set_jit_false(data))
        self.assertFalse(data["server"]["justInTimeModelLoading"])
        untouched = {"port": 1234}
        self.assertFalse(ask_mail._set_jit_false(untouched))


class CliSmokeTests(unittest.TestCase):
    def test_help(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "ask_mail_generate_probes.py"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("probe-f", proc.stdout)


if __name__ == "__main__":
    unittest.main()
