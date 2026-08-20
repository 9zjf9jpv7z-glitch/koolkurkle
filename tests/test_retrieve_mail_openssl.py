#!/usr/bin/env python3
"""Tests for openssl IMAP retrieve. No live iCloud connection."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import retrieve_mail_openssl as r  # noqa: E402

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

FAKE_OPENSSL = r"""#!/usr/bin/env python3
import sys
if "s_client" not in sys.argv:
    sys.stdout.write("OpenSSL 3.0.0\n")
    raise SystemExit(0)
sys.stdout.write("* OK fake imap\r\n")
sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.replace("\r", "").rstrip("\n")
    if not line:
        continue
    parts = line.split(None, 1)
    tag = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    upper = rest.upper()
    if upper.startswith("LOGIN"):
        sys.stdout.write(tag + " OK logged in\r\n")
    elif upper.startswith("LIST"):
        sys.stdout.write('* LIST (\\HasNoChildren) "/" INBOX\r\n')
        sys.stdout.write('* LIST (\\HasNoChildren) "/" Archive\r\n')
        sys.stdout.write('* LIST (\\Noselect) "/" "Skip Me"\r\n')
        sys.stdout.write(tag + " OK list\r\n")
    elif upper.startswith("EXAMINE"):
        sys.stdout.write(tag + " OK [READ-ONLY] examine\r\n")
    elif upper.startswith("UID SEARCH"):
        sys.stdout.write("* SEARCH 96\r\n")
        sys.stdout.write(tag + " OK search\r\n")
    elif upper.startswith("UID FETCH"):
        body = """ + repr(RFC822) + r"""
        raw = body.encode("latin-1")
        sys.stdout.write("* 1 FETCH (UID 96 FLAGS () RFC822.SIZE %d BODY[] {%d}\r\n" % (len(raw), len(raw)))
        sys.stdout.flush()
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.write(b")\r\n")
        sys.stdout.buffer.flush()
        sys.stdout.write(tag + " OK fetch\r\n")
    elif upper.startswith("LOGOUT"):
        sys.stdout.write("* BYE\r\n")
        sys.stdout.write(tag + " OK logout\r\n")
        sys.stdout.flush()
        break
    else:
        sys.stdout.write(tag + " BAD\r\n")
    sys.stdout.flush()
"""


class ParserTests(unittest.TestCase):
    def test_openssl_argv_has_no_user_or_password(self):
        password = "unit-test-app-password"
        argv = r.openssl_argv("/usr/bin/openssl", "imap.mail.me.com", 993)
        joined = " ".join(argv)
        self.assertNotIn(password, joined)
        self.assertNotIn("kirkbacon", joined)
        self.assertEqual(argv[1], "s_client")
        self.assertIn("-connect", argv)
        self.assertIn("imap.mail.me.com:993", argv)

    def test_parse_search_from_star_search(self):
        self.assertEqual(r.parse_search_uids(["* SEARCH 96 97\r\n"]), ["96", "97"])

    def test_record_body(self):
        rec = r.record_from_rfc822("Archive", "96", RFC822, {})
        self.assertIn("Hi there", rec["text"])
        self.assertEqual(rec["raw"], RFC822)


class FakeOpensslRetrieveTests(unittest.TestCase):
    def test_list_only_and_one_message(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake-openssl"
            fake.write_text(FAKE_OPENSSL, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["IMAP_APP_PASSWORD"] = password
            out = tmp_path / "icloud_mail_all.jsonl"

            proc_list = __import__("subprocess").run(
                [
                    sys.executable,
                    str(ROOT / "retrieve_mail_openssl.py"),
                    "--openssl-bin",
                    str(fake),
                    "--list-only",
                    "--output",
                    str(out),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_list.returncode, 2, proc_list.stderr)
            self.assertIn("retrieve_mail_applecurl.py", proc_list.stderr)
            self.assertNotIn(password, proc_list.stdout)
            self.assertNotIn(password, proc_list.stderr)

    def test_fetch_items_peek_and_no_store(self):
        self.assertIn("BODY.PEEK[]", r.FETCH_ITEMS)
        source = (ROOT / "retrieve_mail_openssl.py").read_text()
        self.assertIn("EXAMINE", source)
        self.assertNotRegex(source, r'command\("STORE')
        self.assertNotIn("import imaplib", source)
        self.assertNotIn("IMAP4_SSL(", source)


if __name__ == "__main__":
    unittest.main()
