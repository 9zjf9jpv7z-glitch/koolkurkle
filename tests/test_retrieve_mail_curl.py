#!/usr/bin/env python3
"""Tests for the curl IMAP retrieve path. No live iCloud connection."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import retrieve_mail_curl as r  # noqa: E402
import icloud_mail  # noqa: E402
import move_icloud_junk  # noqa: E402


LIST_SAMPLE = """\
* LIST (\\HasNoChildren) "/" INBOX
* LIST (\\HasNoChildren) "/" "Sent Messages"
* LIST (\\Noselect \\HasChildren) "/" "Skip Me"
* LIST (\\HasNoChildren) "/" "Has Attachments"
"""

SEARCH_SAMPLE = "* SEARCH 11 12 13\n"

FETCH_META_SAMPLE = """\
* 1 FETCH (UID 11 FLAGS (\\Seen) INTERNALDATE "20-Aug-2026 12:00:00 +0000" RFC822.SIZE 123)
* 2 FETCH (FLAGS (\\Flagged) UID 12 INTERNALDATE " 1-Jan-2024 00:00:00 +0000" RFC822.SIZE 9)
"""

RFC822 = (
    "From: Ada <ada@example.com>\r\n"
    "To: Bob <bob@example.com>\r\n"
    "Subject: Hello\r\n"
    "Date: Wed, 20 Aug 2026 12:00:00 +0000\r\n"
    "Message-ID: <msg-11@example.com>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hi there\r\n"
)


class ParserTests(unittest.TestCase):
    def test_parse_list_folders(self):
        folders = r.parse_list_folders(LIST_SAMPLE)
        names = [f["name"] for f in folders]
        self.assertEqual(
            names, ["INBOX", "Sent Messages", "Skip Me", "Has Attachments"]
        )
        by_name = {f["name"]: f for f in folders}
        self.assertTrue(by_name["Skip Me"]["noselect"])
        self.assertFalse(by_name["INBOX"]["noselect"])
        self.assertEqual(by_name["Sent Messages"]["delimiter"], "/")

    def test_parse_search_uids(self):
        self.assertEqual(r.parse_search_uids(SEARCH_SAMPLE), ["11", "12", "13"])
        self.assertEqual(r.parse_search_uids("* SEARCH\n"), [])

    def test_parse_fetch_meta(self):
        meta = r.parse_fetch_meta(FETCH_META_SAMPLE)
        self.assertEqual(meta["11"]["flags"], ["\\Seen"])
        self.assertEqual(meta["11"]["rfc822_size"], 123)
        self.assertEqual(meta["12"]["flags"], ["\\Flagged"])
        self.assertTrue(meta["12"]["internaldate"].strip().startswith("1-Jan-2024"))

    def test_record_from_rfc822(self):
        rec = r.record_from_rfc822(
            folder="INBOX",
            uid="11",
            raw=RFC822,
            meta={"flags": ["\\Seen"], "internaldate": "20-Aug-2026 12:00:00 +0000"},
        )
        self.assertEqual(rec["folder"], "INBOX")
        self.assertEqual(rec["uid"], "11")
        self.assertEqual(rec["from"], "Ada <ada@example.com>")
        self.assertEqual(rec["to"], ["Bob <bob@example.com>"])
        self.assertEqual(rec["subject"], "Hello")
        self.assertIn("Hi there", rec["text"])
        self.assertEqual(rec["raw"], RFC822)
        self.assertTrue(rec["date"].startswith("2026-08-20"))

    def test_extract_rfc822_from_wrapped_fetch(self):
        size = len(RFC822.encode("latin-1"))
        wrapped = f"* 1 FETCH (UID 11 BODY[] {{{size}}}\r\n{RFC822})\r\n"
        self.assertEqual(r.extract_rfc822_from_fetch(wrapped), RFC822)

    def test_extract_rfc822_passthrough_raw_message(self):
        self.assertEqual(r.extract_rfc822_from_fetch(RFC822), RFC822)

    def test_extract_rfc822_rejects_status_only(self):
        with self.assertRaises(r.CurlImapError):
            r.extract_rfc822_from_fetch("* 1 FETCH (UID 11 BODY[] {20}\r\n")


class CurlConfigTests(unittest.TestCase):
    def test_password_only_in_config_not_argv(self):
        password = "not-a-real-secret-xyz"
        config = r.build_curl_config(
            email="kirkbacon@me.com",
            password=password,
            transfers=[{"url": "imaps://imap.mail.me.com:993/", "request": "UID SEARCH ALL"}],
        )
        self.assertIn(password, config)
        self.assertIn("user = ", config)
        argv = r.curl_argv("/usr/bin/curl")
        self.assertEqual(argv[:3], ["/usr/bin/curl", "-K", "-"])
        self.assertNotIn(password, argv)
        joined = " ".join(argv)
        self.assertNotIn(password, joined)
        self.assertIn("UID SEARCH ALL", config)
        self.assertNotIn("STORE", config)
        self.assertNotIn("MOVE", config)

    def test_config_escapes_quotes(self):
        config = r.build_curl_config(
            email="kirkbacon@me.com",
            password='p"w',
            transfers=[{"url": "imaps://example/"}],
        )
        self.assertIn(r'p\"w', config)

    def test_fetch_url_has_no_uid_or_peek_query(self):
        transfer = r.fetch_body_transfer("Archive", "96")
        self.assertEqual(transfer["url"], "imaps://imap.mail.me.com:993/Archive")
        self.assertNotIn("/;UID=", transfer["url"])
        self.assertNotIn(";PEEK=", transfer["url"])
        self.assertEqual(transfer["request"], "UID FETCH 96 (BODY.PEEK[])")
        config = r.build_curl_config(
            email="kirkbacon@me.com",
            password="x",
            transfers=[transfer],
        )
        url_lines = [line for line in config.splitlines() if line.startswith("url")]
        self.assertTrue(url_lines)
        for line in url_lines:
            self.assertNotIn("/;UID=", line)
            self.assertNotIn(";PEEK=", line)
        self.assertIn("UID FETCH 96 (BODY.PEEK[])", config)
        self.assertNotIn("STORE", config)


class CurlScriptRedirectTests(unittest.TestCase):
    def test_cli_points_at_imaplib_retrieve(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "retrieve_mail_curl.py"), "--list-only"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("retrieve_mail_openssl.py", proc.stderr)
        self.assertIn("/usr/bin/python3", proc.stderr)


class LegacyScriptTests(unittest.TestCase):
    def test_icloud_mail_destination_rules(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        attached = SimpleNamespace(
            attachments=["file.pdf"],
            subject="Hello",
            from_="pal@example.com",
            headers={},
            date=now,
        )
        self.assertEqual(icloud_mail.destination_for(attached, now), "Has Attachments")

        receipt = SimpleNamespace(
            attachments=[],
            subject="Your receipt from Apple",
            from_="noreply@email.apple.com",
            headers={},
            date=now,
        )
        self.assertEqual(icloud_mail.destination_for(receipt, now), "Receipts")

        old_unsub = SimpleNamespace(
            attachments=[],
            subject="Weekly",
            from_="news@list.example",
            headers={"List-Unsubscribe": "<mailto:u@example>"},
            date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(icloud_mail.destination_for(old_unsub, now), "Old Unsubscribe")

        keep = SimpleNamespace(
            attachments=[],
            subject="Lunch",
            from_="friend@example.com",
            headers={},
            date=now,
        )
        self.assertIsNone(icloud_mail.destination_for(keep, now))

    def test_junk_jsonl_requires_folder_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "icloud_mail_junk.jsonl"
            path.write_text('{"folder":"INBOX","uid":"5"}\n', encoding="utf-8")
            items = move_icloud_junk.load_targets(path)
            self.assertEqual(items, [{"folder": "INBOX", "uid": "5"}])

    def test_retrieve_mail_314_hard_exit_without_imap_tools(self):
        # The Desktop retrieve script must refuse 3.14 unless overridden.
        # Importing retrieve_mail requires imap-tools; test the helper via source.
        source = (ROOT / "retrieve_mail.py").read_text()
        self.assertIn("MailBoxIPv4", source)
        self.assertIn("17.42.251.69", source)
        self.assertIn("3, 14", source)
        self.assertIn("retrieve_mail_openssl.py", source)
        self.assertIn("mark_seen=False", source)


class NoSecretFileTests(unittest.TestCase):
    def test_repo_has_no_app_password_literals(self):
        skip_parts = {".git", "__pycache__", ".venv"}
        for path in ROOT.rglob("*"):
            if any(part in skip_parts for part in path.parts):
                continue
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".md", ".txt", ".yml", ".yaml", ".json", ""}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(
                text,
                r"IMAP_APP_PASSWORD\s*=\s*['\"][^'\"]+['\"]",
            )

    def test_curl_tool_does_not_import_python_sockets(self):
        source = (ROOT / "retrieve_mail_curl.py").read_text()
        self.assertNotRegex(source, r"^\s*import imaplib\b", re.M)
        self.assertNotRegex(source, r"^\s*import socket\b", re.M)
        self.assertNotRegex(source, r"^\s*import ssl\b", re.M)
        self.assertNotRegex(source, r"^\s*from imaplib\b", re.M)
        self.assertNotRegex(source, r"^\s*from socket\b", re.M)
        self.assertNotRegex(source, r"^\s*from ssl\b", re.M)
        self.assertNotIn("imap_tools", source)


if __name__ == "__main__":
    unittest.main()
