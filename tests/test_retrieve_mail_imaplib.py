#!/usr/bin/env python3
"""Tests for stdlib imaplib retrieve. No live iCloud connection."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import retrieve_mail_imaplib as r  # noqa: E402

RFC822 = (
    "From: Ada <ada@example.com>\r\n"
    "To: Bob <bob@example.com>\r\n"
    "Subject: Hello\r\n"
    "Date: Wed, 20 Aug 2026 12:00:00 +0000\r\n"
    "Message-ID: <msg-96@example.com>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hi there\r\n"
)


class ParserTests(unittest.TestCase):
    def test_parse_list_imaplib_and_star_list(self):
        folders = r.parse_list_folders(
            [
                b'(\\HasNoChildren) "/" INBOX',
                b'* LIST (\\HasNoChildren) "/" "Sent Messages"',
                b'(\\Noselect \\HasChildren) "/" "Skip Me"',
            ]
        )
        names = [f["name"] for f in folders]
        self.assertEqual(names, ["INBOX", "Sent Messages", "Skip Me"])
        self.assertTrue(folders[2]["noselect"])
        self.assertFalse(folders[0]["noselect"])

    def test_parse_search_uids(self):
        self.assertEqual(r.parse_search_uids([b"96 97 102"]), ["96", "97", "102"])
        self.assertEqual(r.parse_search_uids([b""]), [])

    def test_parse_imaplib_fetch_tuple_literal(self):
        raw = RFC822.encode("latin-1")
        header = (
            b"1 (UID 96 FLAGS (\\Seen) INTERNALDATE "
            b'"20-Aug-2026 12:00:00 +0000" RFC822.SIZE %d BODY[] {%d}'
            % (len(raw), len(raw))
        )
        text, meta = r.parse_imaplib_fetch([(header, raw), b")"])
        self.assertEqual(text, RFC822)
        self.assertEqual(meta["uid"], "96")
        self.assertEqual(meta["flags"], ["\\Seen"])
        self.assertEqual(meta["rfc822_size"], len(raw))

    def test_parse_imaplib_fetch_rejects_status_only(self):
        with self.assertRaises(r.ImapError):
            r.parse_imaplib_fetch(
                [b"* 1 FETCH (UID 96 BODY[] {14941242}"]
            )

    def test_parse_imaplib_fetch_rejects_empty_literal(self):
        with self.assertRaises(r.ImapError):
            r.parse_imaplib_fetch([(b"1 (UID 96 BODY[] {0}", b""), b")"])

    def test_quote_mailbox(self):
        self.assertEqual(r.quote_mailbox("INBOX"), "INBOX")
        self.assertEqual(r.quote_mailbox("Archive"), "Archive")
        self.assertEqual(r.quote_mailbox("Sent Messages"), '"Sent Messages"')

    def test_record_has_body(self):
        rec = r.record_from_rfc822("Archive", "96", RFC822, {"flags": []})
        self.assertIn("Hi there", rec["text"])
        self.assertEqual(rec["raw"], RFC822)
        self.assertEqual(rec["from"], "Ada <ada@example.com>")


class FakeIMAP:
    def __init__(self, host, port=993, ssl_context=None, timeout=None):
        self.host = host
        self.port = port
        self.commands = []
        self._selected = None

    def login(self, user, password):
        self.commands.append(("login", user))
        self.password_seen = password
        return "OK", [b"Logged in"]

    def list(self, *args):
        self.commands.append(("list",) + args)
        return "OK", [
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren) "/" Archive',
            b'(\\Noselect) "/" "Skip Me"',
        ]

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        self._selected = mailbox
        if not readonly:
            raise AssertionError("mailbox must be opened readonly")
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        self.commands.append(("uid", cmd) + args)
        if cmd.upper() == "SEARCH":
            return "OK", [b"96"]
        if cmd.upper() == "FETCH":
            if "BODY.PEEK[]" not in args[-1]:
                raise AssertionError("FETCH must use BODY.PEEK[]")
            if "BODY[]" in args[-1] and "BODY.PEEK[]" not in args[-1]:
                raise AssertionError("bare BODY[] would mark seen")
            raw = RFC822.encode("latin-1")
            header = (
                b"1 (UID 96 FLAGS () INTERNALDATE "
                b'"20-Aug-2026 12:00:00 +0000" RFC822.SIZE %d BODY[] {%d}'
                % (len(raw), len(raw))
            )
            return "OK", [(header, raw), b")"]
        return "NO", [b"unexpected"]

    def logout(self):
        self.commands.append(("logout",))
        return "OK", [b"Bye"]


class RetrieveTests(unittest.TestCase):
    def test_list_only_and_max_messages(self):
        password = "unit-test-app-password"
        fake = FakeIMAP("imap.mail.me.com", 993)
        env = os.environ.copy()
        env["IMAP_APP_PASSWORD"] = password
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "icloud_mail_all.jsonl"
            with patch.object(r.imaplib, "IMAP4_SSL", lambda *a, **k: fake):
                with patch.dict(os.environ, {"IMAP_APP_PASSWORD": password}):
                    rc = r.main(
                        ["--list-only", "--output", str(out)]
                    )
            self.assertEqual(rc, 0)
            self.assertIn(("login", r.EMAIL), fake.commands)
            self.assertTrue(any(cmd[0] == "list" for cmd in fake.commands))
            self.assertFalse(out.exists())

            fake2 = FakeIMAP("imap.mail.me.com", 993)
            with patch.object(r.imaplib, "IMAP4_SSL", lambda *a, **k: fake2):
                with patch.dict(os.environ, {"IMAP_APP_PASSWORD": password}):
                    rc = r.main(
                        [
                            "--max-messages",
                            "1",
                            "--output",
                            str(out),
                            "--overwrite",
                        ]
                    )
            self.assertEqual(rc, 0)
            selects = [cmd for cmd in fake2.commands if cmd[0] == "select"]
            self.assertTrue(selects)
            self.assertTrue(all(cmd[2] is True for cmd in selects))
            fetches = [cmd for cmd in fake2.commands if cmd[0] == "uid" and cmd[1] == "FETCH"]
            self.assertTrue(fetches)
            self.assertTrue(all("BODY.PEEK[]" in cmd[-1] for cmd in fetches))
            self.assertFalse(any(cmd[0] == "store" for cmd in fake2.commands))
            lines = [json.loads(line) for line in out.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertTrue(rec["raw"])
            self.assertIn("Hi there", rec["text"])
            self.assertIn(rec["folder"], ("INBOX", "Archive"))

    def test_broken_homebrew_python_refused(self):
        with patch.object(r.sys, "platform", "darwin"):
            with patch.object(r.os.path, "realpath", return_value="/opt/homebrew/bin/python3.11"):
                with self.assertRaises(SystemExit) as ctx:
                    r.reject_broken_mac_python()
        self.assertIn("/usr/bin/python3", str(ctx.exception))

    def test_fetch_items_use_peek(self):
        self.assertIn("BODY.PEEK[]", r.FETCH_ITEMS)
        self.assertNotIn("STORE", r.FETCH_ITEMS)


class NoSecretTests(unittest.TestCase):
    def test_no_password_assignment_in_repo(self):
        text = (ROOT / "retrieve_mail_imaplib.py").read_text()
        self.assertNotRegex(text, r"IMAP_APP_PASSWORD\s*=\s*['\"][^'\"]+['\"]")


if __name__ == "__main__":
    unittest.main()
