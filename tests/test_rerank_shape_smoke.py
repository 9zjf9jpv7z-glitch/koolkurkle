#!/usr/bin/env python3
"""Negative CRM smoke: wrong score shapes fail the PR.

Catches Ollama comma-garbage and missing scores sold as working.
No live weights, no SoR, no network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rerank_lib as rl  # noqa: E402


class ScoreShapeGateTests(unittest.TestCase):
    def test_comma_garbage_is_not_a_score_vector(self):
        with self.assertRaises(rl.RerankError):
            rl.coerce_score_vector("0.12, 0.45, 0.03", 3)
        with self.assertRaises(rl.RerankError):
            rl.parse_rerank_score("0.12, 0.45, 0.03")

    def test_scalar_is_not_n_scores(self):
        with self.assertRaises(rl.RerankError):
            rl.coerce_score_vector(0.91, 2)

    def test_text_and_dict_rejected(self):
        with self.assertRaises(rl.RerankError):
            rl.coerce_score_vector("yes, no, maybe", 3)
        with self.assertRaises(rl.RerankError):
            rl.coerce_score_vector({"scores": [0.1, 0.2]}, 2)

    def test_wrong_length_rejected(self):
        with self.assertRaises(rl.RerankError):
            rl.coerce_score_vector([0.1, 0.2], 3)

    def test_yes_no_logits_pair_accepted(self):
        scores = rl.coerce_score_vector([[2.0, -2.0], [-1.0, 1.0]], 2)
        self.assertEqual(len(scores), 2)
        self.assertGreater(scores[0], 0.5)
        self.assertLess(scores[1], 0.5)

    def test_live_float_list_accepted(self):
        scores = rl.coerce_score_vector([2.4, -3.1], 2)
        self.assertEqual(scores, [2.4, -3.1])


class CrossEncoderInterfaceTests(unittest.TestCase):
    def test_predict_floats_relevant_beats_unrelated(self):
        class FakeCE:
            def predict(self, pairs, **kwargs):
                del kwargs
                out = []
                for _query, doc in pairs:
                    out.append(2.4 if "SDGE" in str(doc) else -3.1)
                return out

        scores = rl.score_documents(
            "SDGE bill",
            ["SDGE bill due Friday", "horse boarding newsletter"],
            backend="crossencoder",
            encoder=FakeCE(),
        )
        self.assertEqual(len(scores), 2)
        self.assertTrue(all(rl.is_live_score(s) for s in scores))
        self.assertGreater(scores[0], scores[1])

    def test_predict_garbage_raises(self):
        class BadCE:
            def predict(self, pairs, **kwargs):
                del pairs, kwargs
                return "0.12, 0.45"

        with self.assertRaises(rl.RerankError):
            rl.score_documents(
                "q",
                ["a", "b"],
                backend="crossencoder",
                encoder=BadCE(),
            )

    def test_missing_sentence_transformers_raises(self):
        with mock.patch.object(
            rl,
            "_import_cross_encoder",
            side_effect=rl.RerankError(
                "sentence_transformers not installed (optional extra)"
            ),
        ):
            with self.assertRaises(rl.RerankError) as ctx:
                rl.score_documents_crossencoder("q", ["a"])
        self.assertIn("sentence_transformers", str(ctx.exception))


class SmokeScriptTests(unittest.TestCase):
    def test_smoke_exits_zero_and_labels_modes(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "rerank_smoke.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload.get("ok"))
        self.assertIn(payload.get("rerank_mode"), ("crossencoder", "fail_open"))
        self.assertEqual(payload.get("generate_mode"), "off")
        self.assertIn("needed_signal", payload)
        self.assertTrue(payload["negative_smoke"]["comma_garbage_rejected"])
        if not payload.get("weights_available"):
            self.assertTrue(payload.get("fail_open_only"))
            self.assertIn("fail-open-only", payload.get("label", ""))
        relevant = payload["example"]["relevant"]["score"]
        unrelated = payload["example"]["unrelated"]["score"]
        self.assertGreater(relevant, unrelated)

    def test_fixture_documents_interface(self):
        path = ROOT / "tests" / "fixtures" / "rerank_interface_proof.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("yes/no logits", data["needed_signal"])
        self.assertIn("CrossEncoder.predict", data["needed_signal"])
        self.assertEqual(data["pairs"][0]["label"], "relevant")
        self.assertEqual(data["pairs"][1]["label"], "unrelated")
        self.assertGreater(data["pairs"][0]["stub_score"], data["pairs"][1]["stub_score"])


if __name__ == "__main__":
    unittest.main()
