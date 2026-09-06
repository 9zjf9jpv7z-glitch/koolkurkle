#!/usr/bin/env python3
"""PR-7 Qwen3-Reranker helper tests. Mocks only — no live Ollama, no SoR."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rerank_lib as rl  # noqa: E402


class ParseScoreTests(unittest.TestCase):
    def test_yes_no_and_think_strip(self):
        self.assertEqual(rl.parse_rerank_score("yes"), 1.0)
        self.assertEqual(rl.parse_rerank_score("No."), 0.0)
        self.assertEqual(
            rl.parse_rerank_score("<think>reasoning</think>\nyes"), 1.0
        )

    def test_numeric_and_logprobs(self):
        self.assertAlmostEqual(rl.parse_rerank_score("0.73"), 0.73)
        score = rl.parse_rerank_score(
            "maybe",
            {
                "logprobs": [
                    {"token": "yes", "logprob": -0.2},
                    {"token": "no", "logprob": -2.0},
                ]
            },
        )
        self.assertGreater(score, 0.5)
        self.assertLessEqual(score, 1.0)

    def test_empty_raises(self):
        with self.assertRaises(rl.RerankError):
            rl.parse_rerank_score("   ")


class DocumentAndPromptTests(unittest.TestCase):
    def test_hit_document_uses_subject_snippet_not_body(self):
        text = rl.hit_document(
            {
                "subject": "SDGE bill",
                "snippet": "due Friday",
                "body": "SECRET-BODY-NOT-FOR-RERANK",
            }
        )
        self.assertIn("SDGE bill", text)
        self.assertIn("due Friday", text)
        self.assertNotIn("SECRET-BODY-NOT-FOR-RERANK", text)

    def test_prompt_is_official_qwen3_shape(self):
        prompt = rl.official_generate_prompt("Caddell", "call notes")
        self.assertIn("<Instruct>:", prompt)
        self.assertIn("<Query>: Caddell", prompt)
        self.assertIn("<Document>: call notes", prompt)
        self.assertIn(rl.RERANK_SYSTEM[:20], prompt)


class OllamaClientTests(unittest.TestCase):
    def test_generate_yes_scores_one(self):
        captured: dict = {}

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout

            class Resp:
                status = 200
                code = 200

                def read(self):
                    return json.dumps({"response": "yes"}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Resp()

        score = rl.score_one(
            "SDGE bill",
            "SDGE bill\ndue Friday",
            model="qwen3-reranker:0.6b",
            ollama_url="http://127.0.0.1:11434",
            opener=opener,
            timeout=5,
        )
        self.assertEqual(score, 1.0)
        self.assertTrue(captured["url"].endswith("/api/generate"))
        self.assertEqual(captured["payload"]["model"], "qwen3-reranker:0.6b")
        self.assertTrue(captured["payload"].get("raw"))
        self.assertNotIn("SECRET", json.dumps(captured["payload"]))

    def test_env_model_and_timeout(self):
        with mock.patch.dict(
            "os.environ",
            {"MAILROOM_RERANK_MODEL": "custom-rerank:tag", "MAILROOM_RERANK_TIMEOUT": "9"},
            clear=False,
        ):
            self.assertEqual(rl.default_rerank_model(), "custom-rerank:tag")
            self.assertEqual(rl.default_rerank_timeout(), 9)


class HygieneTests(unittest.TestCase):
    def test_no_machine_homes_or_secrets(self):
        paths = [
            SCRIPTS / "rerank_lib.py",
            SCRIPTS / "semantic_search.py",
            ROOT / "docs" / "rerank.md",
            ROOT / "README.md",
        ]
        forbidden = (
            "/Users/buck",
            "kirkbacon",
            "@me.com",
            "-----BEGIN",
            "ak_live",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, msg="%s %s" % (path.name, token))
        self.assertIn("$HOME", (ROOT / "docs" / "rerank.md").read_text(encoding="utf-8"))
        self.assertIn("MAILROOM_DB", (ROOT / "docs" / "rerank.md").read_text(encoding="utf-8"))
        self.assertIn("MBP", (ROOT / "docs" / "rerank.md").read_text(encoding="utf-8"))
        self.assertIn("Mini", (ROOT / "docs" / "rerank.md").read_text(encoding="utf-8"))
        self.assertIn("ollama pull", (ROOT / "docs" / "rerank.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
