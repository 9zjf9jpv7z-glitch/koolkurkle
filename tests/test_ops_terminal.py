#!/usr/bin/env python3
"""Contract tests for human Terminal / Mac ops docs. No network, no secrets."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "docs" / "ops-terminal.md"
DAILY = ROOT / "scripts" / "README.mailroom-daily.md"
README = ROOT / "README.md"
RERANK = ROOT / "docs" / "rerank.md"
SLIM = ROOT / "macos-slim" / "README.md"
HEALTH = ROOT / "docs" / "sor-health.md"
LOCK = ROOT / "docs" / "pr0" / "with_writer_lock_DESIGN.md"
GATES = ROOT / "docs" / "model-runtime-gates.md"
ASK = ROOT / "docs" / "ask_mail.md"


class OpsTerminalDocTests(unittest.TestCase):
    def test_covers_required_topics(self):
        text = OPS.read_text(encoding="utf-8")
        self.assertIn("One machine per card", text)
        self.assertIn("one Action-required card at a time", text)
        self.assertIn("MBP", text)
        self.assertIn("Mini", text)
        self.assertIn("one command per fence", text)
        self.assertIn("security add-generic-password -a \"$USER\" -s mailroom.imap.app-password -w", text)
        self.assertIn("wc -c", text)
        self.assertIn("mailroom.imap.app-password", text)
        self.assertIn("mailroom.icloud.app-password", text)
        self.assertIn("16–19", text)
        self.assertIn("appleid.apple.com", text)
        self.assertIn("Login denied", text)
        self.assertIn("EXAMPLE_USER_LOCAL", text)
        self.assertIn("example.invalid", text)
        self.assertIn("$HOME", text)
        self.assertIn("__HOME__", text)
        self.assertIn("USERNAME", text)
        self.assertIn("Conversation comment", text)
        self.assertIn("title pencil", text)
        self.assertIn("Little Snitch", text)
        self.assertIn("registry.ollama.ai:443", text)
        self.assertIn("bad file descriptor", text)
        self.assertIn("dengcao/Qwen3-Reranker-0.6B:Q8_0", text)
        self.assertIn(":F16", text)
        self.assertNotIn("dengcao/Qwen3-Reranker-0.6B &&", text)
        self.assertNotIn("ollama pull dengcao", text)
        self.assertNotIn("ollama cp dengcao", text)
        self.assertIn("Early-error traps", text)
        self.assertIn("Interface proof", text)
        self.assertIn("fail-open-only", text)
        self.assertIn("model-runtime-gates.md", text)
        self.assertIn("ask_mail.py --probe", text)
        self.assertIn("qwen3-embedding:8b", text)
        self.assertIn("$HOME/Desktop/Heavy-Bot/to-bot", text)
        self.assertIn("/workspace", text)
        self.assertIn("before box read", text)
        self.assertIn("ollama stop qwen3-embedding:8b", text)
        self.assertIn("--phase retrieve", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("-----BEGIN", text)
        self.assertNotIn("ak_live", text)

    def test_linked_from_existing_docs(self):
        for path in (README, DAILY, RERANK, SLIM, HEALTH, LOCK, GATES, ASK):
            text = path.read_text(encoding="utf-8")
            self.assertIn("ops-terminal.md", text, msg=path.name)

    def test_daily_readme_keeps_legacy_fallback(self):
        text = DAILY.read_text(encoding="utf-8")
        self.assertIn("mailroom.icloud.app-password", text)
        self.assertIn("read fallback", text)
        self.assertIn("keep the legacy item until IMAP", text)
        self.assertIn("# MBP — create IMAP Keychain item", text)
        self.assertIn("# Mini — create IMAP Keychain item", text)
        self.assertNotIn("EXAMPLE_USER_LOCAL", text)
        self.assertNotIn("@example.invalid", text)
        self.assertNotIn("/Users/USERNAME", text)


if __name__ == "__main__":
    unittest.main()
