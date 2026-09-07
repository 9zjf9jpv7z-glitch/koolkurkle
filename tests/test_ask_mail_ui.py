#!/usr/bin/env python3
"""Thin loopback UI + MCP wiring. Mocks only -- no live generate, no SoR."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = Path(__file__).resolve().parent
for extra in (str(SCRIPTS), str(TESTS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ask_mail  # noqa: E402
import ask_mail_ui as ask_ui  # noqa: E402
from test_ask_mail import _fake_retrieve, _make_db  # noqa: E402

LT = chr(60)
AMP = chr(38)


class PageBytesTests(unittest.TestCase):
    def test_page_decodes_and_keeps_citations_first_class(self) -> None:
        page = ask_ui.page_text()
        self.assertGreater(len(page), 2000)
        self.assertEqual(ask_ui.UI_PATH, "/ui")
        self.assertEqual(ask_ui.PROCESS, "mlx_lm.server")
        self.assertEqual(ask_ui.PATH_FAIL_OPEN, "fail-open-only")
        self.assertTrue(ask_ui.CONTENT_TYPE.startswith("text/html"))
        for token in (
            "Mailroom ask",
            "Citations",
            "fail-open-only",
            "mlx_lm.server",
            "generate_process",
            "generate_mode",
            "fetch(\"/ask\"",
            "fetch(\"/health\"",
            "Same-origin POST /ask",
            "Citations stay visible",
            "--mcp",
        ):
            self.assertIn(token, page)
        self.assertNotIn("attachment", page.lower())
        self.assertNotIn("/Users/", page)
        self.assertNotIn("@me.com", page)

    def test_banner_is_empty_sibling_so_fail_open_cannot_wipe_citations(self) -> None:
        page = ask_ui.page_text()
        empty_banner = 'id="banner"' + chr(62) + LT + "/div" + chr(62)
        self.assertIn(empty_banner, page)
        banner_at = page.find('id="banner"')
        cites_at = page.find('id="citations"')
        banner_close = page.find(LT + "/div" + chr(62), banner_at)
        self.assertGreater(cites_at, banner_close)
        self.assertIn('id="answer"', page)
        self.assertIn('id="hits"', page)
        self.assertIn('id="labels"', page)

    def test_js_esc_entities_survived_encode(self) -> None:
        page = ask_ui.page_text()
        self.assertIn(AMP + "amp;", page)
        self.assertIn(AMP + "lt;", page)
        self.assertIn(AMP + "gt;", page)
        self.assertIn(AMP + "quot;", page)

    def test_python_sources_have_no_markup_delimiter(self) -> None:
        ui_src = (SCRIPTS / "ask_mail_ui.py").read_text(encoding="utf-8")
        cli_src = (SCRIPTS / "ask_mail.py").read_text(encoding="utf-8")
        self.assertEqual(ui_src.count(LT), 0)
        self.assertEqual(cli_src.count(LT), 0)
        self.assertIn("def ask(", cli_src)
        self.assertGreater((SCRIPTS / "ask_mail.py").stat().st_size, 10000)
        self.assertNotIn("LOADED_FROM_MCP_PUSH_ASK_JSON", cli_src)


class HttpUiTests(unittest.TestCase):
    def _serve(self, tmp: str):
        db = Path(tmp) / "mailroom.sqlite"
        _make_db(db)
        cfg = {
            "db": db,
            "k": 5,
            "generate": False,
            "retrieve_fn": _fake_retrieve(["m1", "m4"]),
            "audit": False,
        }
        httpd = ask_mail.bind_http_server("127.0.0.1", 0, 0, cfg)
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd

    def test_get_ui_and_health_and_root_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self._serve(tmp)
            port = httpd.server_address[1]
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/ui")
                resp = conn.getresponse()
                body = resp.read()
                ctype = resp.getheader("Content-Type") or ""
                conn.close()
                self.assertEqual(resp.status, 200)
                self.assertTrue(ctype.startswith("text/html"))
                page = body.decode("utf-8")
                self.assertIn("Citations", page)
                self.assertIn("fail-open-only", page)
                self.assertEqual(body, ask_ui.page_bytes())

                alias = HTTPConnection("127.0.0.1", port, timeout=5)
                alias.request("GET", "/ui.html")
                aresp = alias.getresponse()
                self.assertEqual(aresp.status, 200)
                self.assertEqual(aresp.read(), ask_ui.page_bytes())
                alias.close()

                health = HTTPConnection("127.0.0.1", port, timeout=5)
                health.request("GET", "/health")
                hresp = health.getresponse()
                hbody = json.loads(hresp.read().decode("utf-8"))
                health.close()
                self.assertEqual(hresp.status, 200)
                self.assertTrue(hbody["ok"])
                self.assertEqual(hbody["ui"], "/ui")
                self.assertEqual(hbody["generate_process"], "mlx_lm.server")
                self.assertEqual(hbody["generate_runtime"], "mlx_lm.server")

                root = HTTPConnection("127.0.0.1", port, timeout=5)
                root.request("GET", "/")
                rresp = root.getresponse()
                rbody = json.loads(rresp.read().decode("utf-8"))
                root.close()
                self.assertEqual(rresp.status, 200)
                self.assertEqual(rbody["service"], "ask_mail")
                self.assertEqual(rbody["ui"], "/ui")
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_post_ask_still_returns_citations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self._serve(tmp)
            port = httpd.server_address[1]
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "POST",
                    "/ask",
                    body=json.dumps({"query": "invoice", "k": 5}),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                payload = json.loads(resp.read().decode("utf-8"))
                conn.close()
                self.assertEqual(resp.status, 200)
                self.assertEqual(payload["citations"], ["m1", "m4"])
                self.assertEqual(payload["generate_mode"], "hits_only")
                self.assertIn("rerank_mode", payload)
                self.assertEqual(payload["generate_process"], "mlx_lm.server")
            finally:
                httpd.shutdown()
                httpd.server_close()


class McpStillWorksTests(unittest.TestCase):
    def test_four_tools_and_ask_mail_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailroom.sqlite"
            _make_db(db)
            cfg = {
                "db": db,
                "k": 5,
                "generate": False,
                "retrieve_fn": _fake_retrieve(["m1"]),
                "audit": False,
                "drafts_dir": Path(tmp) / "drafts",
            }
            listed = ask_mail.handle_mcp_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, cfg
            )
            tools = listed["result"]["tools"]
            names = [t["name"] for t in tools]
            self.assertEqual(
                names, ["ask_mail", "hybrid_search", "get_thread", "draft_reply"]
            )
            ask_tool = tools[0]
            self.assertIn("mlx_lm.server", ask_tool["description"])
            called = ask_mail.handle_mcp_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "ask_mail", "arguments": {"query": "invoice"}},
                },
                cfg,
            )
            body = json.loads(called["result"]["content"][0]["text"])
            self.assertEqual(body["generate_mode"], "hits_only")
            self.assertEqual(body["citations"], ["m1"])


class HygieneAndDocsTests(unittest.TestCase):
    def test_ui_paths_have_no_pii(self) -> None:
        paths = [
            SCRIPTS / "ask_mail_ui.py",
            SCRIPTS / "ask_mail.py",
            SCRIPTS / "install-mlx-generate.sh",
            ROOT / "docs" / "ask_mail.md",
            ROOT / "docs" / "generate-mlx.md",
            ROOT / "README.md",
        ]
        forbidden = (
            "/Users/",
            "@me.com",
            "EXAMPLE_USER_LOCAL",
            "@example.invalid",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, msg="%s %s" % (path.name, token))
        cli = (SCRIPTS / "ask_mail.py").read_text(encoding="utf-8")
        ui = (SCRIPTS / "ask_mail_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("LOADED_FROM_MCP_PUSH_ASK_JSON", cli)
        self.assertNotIn("LOADED_FROM_MCP_PUSH_ASK_JSON", ui)

    def test_docs_name_ui_mcp_and_three_processes(self) -> None:
        ask_docs = (ROOT / "docs" / "ask_mail.md").read_text(encoding="utf-8")
        gen_docs = (ROOT / "docs" / "generate-mlx.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        installer = (SCRIPTS / "install-mlx-generate.sh").read_text(encoding="utf-8")
        for text in (ask_docs, gen_docs, readme):
            self.assertIn("GET /ui", text)
            self.assertIn("--mcp", text)
            self.assertIn("mlx_lm.server", text)
            self.assertIn("fail-open-only", text)
            self.assertIn("install-mlx-generate.sh", text)
        self.assertIn("ask_mail_ui.py", gen_docs)
        self.assertIn("ask_mail_ui.py", installer)
        self.assertIn("two processes", ask_docs.lower())
        self.assertIn("Citations", ask_docs)
        self.assertIn("no attachment ingest", ask_docs.lower())
        self.assertIn("fail-open-only", gen_docs)
        self.assertIn("not `kill`", gen_docs)


if __name__ == "__main__":
    unittest.main()
