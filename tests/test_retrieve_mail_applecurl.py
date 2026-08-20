#!/usr/bin/env python3
"""Tests for Apple-curl native UID retrieve. No live iCloud."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import retrieve_mail_applecurl as r  # noqa: E402

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

LIST_SAMPLE = """\
* LIST (\\HasNoChildren) "/" INBOX
* LIST (\\HasNoChildren) "/" Archive
* LIST (\\Noselect) "/" "Skip Me"
"""


class UrlTests(unittest.TestCase):
    def test_native_uid_url_has_no_peek(self):
        url = r.message_url("Archive", "96", "noslash")
        self.assertEqual(url, "imaps://imap.mail.me.com:993/Archive;UID=96")
        self.assertNotIn(";PEEK=", url)
        self.assertNotIn("/;UID=", url)
        slash = r.message_url("Archive", "96", "slash")
        self.assertEqual(slash, "imaps://imap.mail.me.com:993/Archive/;UID=96")
        self.assertNotIn(";PEEK=", slash)

    def test_body_config_is_native_url_not_custom_fetch(self):
        transfer = {
            "url": r.message_url("Archive", "96", "noslash"),
            "write_out": "SEP",
        }
        config = r.build_curl_config("kirkbacon@me.com", "x", [transfer])
        url_lines = [line for line in config.splitlines() if line.startswith("url")]
        self.assertTrue(url_lines)
        for line in url_lines:
            self.assertIn("Archive;UID=96", line)
            self.assertNotIn(";PEEK=", line)
            self.assertNotIn("/;UID=", line)
        self.assertNotIn("BODY.PEEK[]", config)
        self.assertNotIn("request =", config)

    def test_extract_rejects_zero_byte_literal(self):
        with self.assertRaises(r.CurlImapError):
            r.extract_rfc822("* 1 FETCH (UID 96 BODY[] {14941242}\r\n")

    def test_extract_passthrough_rfc822(self):
        self.assertEqual(r.extract_rfc822(RFC822), RFC822)

    def test_password_not_in_argv(self):
        password = "unit-test-app-password"
        argv = r.curl_argv("/usr/bin/curl")
        self.assertEqual(argv[:3], ["/usr/bin/curl", "-K", "-"])
        self.assertNotIn(password, argv)


class FakeCurlTests(unittest.TestCase):
    def test_list_and_one_message_via_native_uid(self):
        password = "unit-test-app-password"
        fake_src = (
            "#!/usr/bin/env python3\n"
            "import pathlib, re, sys\n"
            "if '--version' in sys.argv:\n"
            "    sys.stdout.write('curl 8.7.1\\nProtocols: imap imaps\\n')\n"
            "    raise SystemExit(0)\n"
            "cfg = sys.stdin.read()\n"
            "pathlib.Path(sys.argv[0]).with_name('argv.txt').write_text('\\0'.join(sys.argv))\n"
            "pathlib.Path(sys.argv[0]).with_name('last.cfg').write_text(cfg)\n"
            "if 'BODY.PEEK[]' in cfg and 'request' in cfg:\n"
            "    sys.stdout.write('* 1 FETCH (UID 96 BODY[] {20}\\r\\n')\n"
            "    raise SystemExit(0)\n"
            "if 'UID SEARCH ALL' in cfg:\n"
            "    sys.stdout.write('* SEARCH 96\\n')\n"
            "elif 'UID FETCH 1:*' in cfg:\n"
            "    sys.stdout.write('* 1 FETCH (UID 96 FLAGS () RFC822.SIZE 20)\\n')\n"
            "elif 'UID STORE' in cfg:\n"
            "    sys.stdout.write('')\n"
            "elif ';UID=96' in cfg and ';PEEK=' not in cfg:\n"
            "    sys.stdout.write(" + repr(RFC822) + ")\n"
            "    m = re.search(r'write-out = \"(.*)\"', cfg)\n"
            "    if m:\n"
            "        sys.stdout.write(m.group(1))\n"
            "else:\n"
            "    sys.stdout.write(" + repr(LIST_SAMPLE) + ")\n"
            "sys.exit(0)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake-curl"
            fake.write_text(fake_src, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["IMAP_APP_PASSWORD"] = password
            out = tmp_path / "icloud_mail_all.jsonl"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "retrieve_mail_applecurl.py"),
                    "--curl-bin",
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
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Archive", proc.stdout)

            proc2 = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "retrieve_mail_applecurl.py"),
                    "--curl-bin",
                    str(fake),
                    "--max-messages",
                    "1",
                    "--overwrite",
                    "--output",
                    str(out),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            argv_text = (tmp_path / "argv.txt").read_text()
            self.assertNotIn(password, argv_text)
            cfg = (tmp_path / "last.cfg").read_text()
            self.assertIn(password, cfg)
            self.assertNotIn(";PEEK=", cfg)
            if "UID STORE" not in cfg:
                self.assertIn(";UID=96", cfg)
                self.assertNotIn("BODY.PEEK[]", cfg)
            lines = [json.loads(line) for line in out.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertTrue(rec["raw"])
            self.assertIn("Hi there", rec["text"])


if __name__ == "__main__":
    unittest.main()
