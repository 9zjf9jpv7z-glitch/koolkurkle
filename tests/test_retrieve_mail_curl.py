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

    def test_urls_use_peek_and_imaps(self):
        url = r.message_url("Sent Messages", "42")
        self.assertTrue(url.startswith("imaps://imap.mail.me.com:993/"))
        self.assertIn("Sent%20Messages", url)
        self.assertIn(";UID=42", url)
        self.assertIn(";PEEK=1", url)
        self.assertNotIn("BODY[]", url)


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

    def test_peek_url_in_body_transfer(self):
        config = r.build_curl_config(
            email="kirkbacon@me.com",
            password="x",
            transfers=[{"url": r.message_url("INBOX", "9"), "write_out": "SEP"}],
        )
        self.assertIn(";PEEK=1", config)
        self.assertNotIn("STORE", config)


class FakeCurlRetrieveTests(unittest.TestCase):
    def test_list_and_retrieve_via_fake_curl(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_curl = tmp_path / "fake-curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, re, sys\n"
                "if '--version' in sys.argv:\n"
                "    sys.stdout.write('curl 8.0.0\\nProtocols: imap imaps\\n')\n"
                "    raise SystemExit(0)\n"
                "cfg = sys.stdin.read()\n"
                "argv_path = pathlib.Path(sys.argv[0]).with_name('argv.txt')\n"
                "argv_path.write_text('\\0'.join(sys.argv))\n"
                "cfg_path = pathlib.Path(sys.argv[0]).with_name('last.cfg')\n"
                "cfg_path.write_text(cfg)\n"
                "if 'UID SEARCH ALL' in cfg:\n"
                "    sys.stdout.write('* SEARCH 11\\n')\n"
                "elif 'UID FETCH 1:*' in cfg:\n"
                "    sys.stdout.write("
                + repr(
                    '* 1 FETCH (UID 11 FLAGS (\\Seen) INTERNALDATE '
                    '"20-Aug-2026 12:00:00 +0000" RFC822.SIZE 20)\\n'
                )
                + ")\n"
                "elif ';UID=11;PEEK=1' in cfg:\n"
                "    sys.stdout.write(" + repr(RFC822) + ")\n"
                "    m = re.search(r'write-out = \"(.*)\"', cfg)\n"
                "    if m:\n"
                "        sys.stdout.write(m.group(1))\n"
                "else:\n"
                "    sys.stdout.write(" + repr(LIST_SAMPLE) + ")\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env["IMAP_APP_PASSWORD"] = password
            env.pop("CURL_BIN", None)
            out = tmp_path / "icloud_mail_all.jsonl"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "retrieve_mail_curl.py"),
                    "--curl-bin",
                    str(fake_curl),
                    "--output",
                    str(out),
                    "--overwrite",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_path),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            argv_text = (tmp_path / "argv.txt").read_text()
            self.assertNotIn(password, argv_text)
            self.assertIn("-K", argv_text)
            cfg = (tmp_path / "last.cfg").read_text()
            # last transfer is a body fetch; password stays in config only
            self.assertIn(password, cfg)
            self.assertIn(";PEEK=1", cfg)

            lines = [json.loads(line) for line in out.read_text().splitlines() if line]
            self.assertGreaterEqual(len(lines), 1)
            rec = next(item for item in lines if item["uid"] == "11")
            self.assertEqual(rec["folder"], "INBOX")
            self.assertIn("Hi there", rec["text"])
            self.assertEqual(rec["flags"], ["\\Seen"])

            # Resume should write zero new rows.
            proc2 = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "retrieve_mail_curl.py"),
                    "--curl-bin",
                    str(fake_curl),
                    "--output",
                    str(out),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(tmp_path),
            )
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("0 new", proc2.stderr)
            self.assertEqual(len(out.read_text().splitlines()), len(lines))


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
        self.assertIn("retrieve_mail_curl.py", source)
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
