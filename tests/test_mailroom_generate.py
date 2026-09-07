"""Unit tests for mailroom_generate. No live models, no Ollama generate."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mailroom_generate as mg  # noqa: E402


class ClampTests(unittest.TestCase):
    def test_default(self) -> None:
        self.assertEqual(mg.clamp_max_tokens(None), 768)

    def test_floor_and_ceiling(self) -> None:
        self.assertEqual(mg.clamp_max_tokens(1), 512)
        self.assertEqual(mg.clamp_max_tokens(9999), 1024)
        self.assertEqual(mg.clamp_max_tokens(800), 800)


class PayloadTests(unittest.TestCase):
    def test_payload_sets_max_tokens_and_thinking_off(self) -> None:
        payload = mg.build_chat_payload(
            [{"role": "user", "content": "q"}],
            model="mailroom-generate",
            max_tokens_value=600,
        )
        self.assertEqual(payload["model"], "mailroom-generate")
        self.assertEqual(payload["max_tokens"], 600)
        self.assertFalse(payload["enable_thinking"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["reasoning"]["enabled"], False)

    def test_payload_can_drop_thinking_fields(self) -> None:
        payload = mg.build_chat_payload(
            [{"role": "user", "content": "q"}],
            include_thinking_off=False,
        )
        self.assertNotIn("enable_thinking", payload)
        self.assertIn("max_tokens", payload)


class FailOpenTests(unittest.TestCase):
    def test_label_is_fail_open_only(self) -> None:
        result = mg.fail_open_result(["hit"], reason="down")
        self.assertEqual(result["path"], "fail-open-only")
        self.assertTrue(result["fail_open"])
        self.assertIsNone(result["answer"])
        self.assertEqual(result["hits"], ["hit"])

    def test_live_label_is_llmster_headless(self) -> None:
        result = mg.live_result("ok", ["hit"])
        self.assertEqual(result["path"], "llmster-headless")
        self.assertFalse(result["fail_open"])


class ClientGateTests(unittest.TestCase):
    def test_generate_from_hits_does_not_post_if_identifier_missing(self) -> None:
        with patch.object(mg, "loaded_model_ids", return_value=["other-model"]):
            with patch.object(mg, "post_chat") as post:
                with self.assertRaises(mg.GenerateDown):
                    mg.generate_from_hits("q", ["h"])
                post.assert_not_called()

    def test_ask_mail_live_fail_open_when_generate_down(self) -> None:
        def retrieve(_q: str) -> list[str]:
            return ["raw"]

        def rerank(_q: str, raw: list[str]) -> list[str]:
            return [f"reranked:{raw[0]}"]

        with patch.object(mg, "unload_embed"), patch.object(
            mg, "generate_from_hits", side_effect=mg.GenerateDown("refused")
        ):
            result = mg.ask_mail_live("q", retrieve=retrieve, rerank=rerank)
        self.assertEqual(result["path"], "fail-open-only")
        self.assertEqual(result["hits"], ["reranked:raw"])
        self.assertIsNone(result["answer"])

    def test_ask_mail_live_success_path_label(self) -> None:
        with patch.object(mg, "unload_embed"), patch.object(
            mg, "generate_from_hits", return_value="answer text"
        ):
            result = mg.ask_mail_live(
                "q",
                retrieve=lambda q: ["a"],
                rerank=lambda q, r: r,
            )
        self.assertEqual(result["path"], "llmster-headless")
        self.assertEqual(result["answer"], "answer text")

    def test_400_retries_without_thinking_fields(self) -> None:
        payload = mg.build_chat_payload([{"role": "user", "content": "q"}])
        calls: list[dict] = []

        def fake_http(url, *, method="GET", body=None, timeout=120.0):
            calls.append(body or {})
            if "enable_thinking" in (body or {}):
                return 400, {"error": "unknown field"}
            return 200, {
                "choices": [{"message": {"content": "ok"}}],
            }

        with patch.object(mg, "_http_json", side_effect=fake_http):
            text = mg.post_chat(payload)
        self.assertEqual(text, "ok")
        self.assertEqual(len(calls), 2)
        self.assertIn("enable_thinking", calls[0])
        self.assertNotIn("enable_thinking", calls[1])
        self.assertIn("max_tokens", calls[1])


class ModelsUrlTests(unittest.TestCase):
    def test_models_url_from_chat_completions(self) -> None:
        with patch.dict(
            "os.environ",
            {"MAILROOM_GENERATE_URL": "http://127.0.0.1:1234/v1/chat/completions"},
            clear=False,
        ):
            self.assertEqual(mg.generate_models_url(), "http://127.0.0.1:1234/v1/models")


class OllamaPsEmptyTests(unittest.TestCase):
    def test_header_only_is_empty(self) -> None:
        with patch.object(mg, "ollama_ps_text", return_value="NAME\tID\tSIZE\n"):
            self.assertTrue(mg.ollama_ps_empty())

    def test_loaded_row_is_not_empty(self) -> None:
        with patch.object(
            mg, "ollama_ps_text", return_value="NAME\tID\nqwen3-embed:latest\tabc\n"
        ):
            self.assertFalse(mg.ollama_ps_empty())


class HitsListTests(unittest.TestCase):
    def test_dict_hits(self) -> None:
        self.assertEqual(mg._hits_list({"hits": [1, 2]}), [1, 2])


class OllamaEmbedOnlyTests(unittest.TestCase):
    def test_refuses_generate_and_run(self) -> None:
        with self.assertRaises(mg.GenerateDown):
            mg._ollama_cmd(["ollama", "generate", "qwen"], timeout=1)
        with self.assertRaises(mg.GenerateDown):
            mg._ollama_cmd(["ollama", "run", "qwen"], timeout=1)
        with self.assertRaises(mg.GenerateDown):
            mg._ollama_cmd(["ollama", "chat"], timeout=1)

    def test_allows_ps_and_stop(self) -> None:
        with patch.object(mg.subprocess, "run") as run:
            run.return_value = type("R", (), {"returncode": 0, "stdout": "NAME\n", "stderr": ""})()
            mg._ollama_cmd(["ollama", "ps"], timeout=1)
            mg._ollama_cmd(["ollama", "stop", "embed"], timeout=1)
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
