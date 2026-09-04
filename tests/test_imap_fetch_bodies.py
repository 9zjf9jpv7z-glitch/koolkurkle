#!/usr/bin/env python3
"""Unit tests for mailroom IMAP body fetch. No live iCloud connection."""

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
from string import Template

ROOT = Path(__file__).resolve().parents[1]
MAILROOM = ROOT / "mailroom"
if str(MAILROOM) not in sys.path:
    sys.path.insert(0, str(MAILROOM))

import imap_fetch_bodies as r  # noqa: E402

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

MULTIPART = (
    "From: Ada <ada@example.com>\r\n"
    "To: Bob <bob@example.com>\r\n"
    "Subject: =?utf-8?q?Caf=C3=A9?=\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/alternative; boundary=bound\r\n"
    "\r\n"
    "--bound\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Plain body\r\n"
    "--bound\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<p>HTML body</p>\r\n"
    "--bound\r\n"
    "Content-Type: application/pdf\r\n"
    "Content-Disposition: attachment; filename=x.pdf\r\n"
    "\r\n"
    "%PDF-not-a-real-file\r\n"
    "--bound--\r\n"
)

HTML_ONLY = (
    "From: Ada <ada@example.com>\r\n"
    "Subject: HtmlOnly\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<html><body><p>Hello&nbsp;world</p><script>alert(1)</script></body></html>\r\n"
)

LIST_SAMPLE = """\
* LIST (\\HasNoChildren) "/" INBOX
* LIST (\\HasNoChildren) "/" "Sent Messages"
* LIST (\\Noselect \\HasChildren) "/" "Skip Me"
* LIST (\\HasNoChildren) "/" Archive
"""

SEARCH_SAMPLE = "* SEARCH 11 12 13\n"

FETCH_META_SAMPLE = """\
* 1 FETCH (UID 11 FLAGS (\\Seen) INTERNALDATE "20-Aug-2026 12:00:00 +0000" RFC822.SIZE 123)
* 2 FETCH (FLAGS (\\Flagged) UID 12 INTERNALDATE " 1-Jan-2024 00:00:00 +0000" RFC822.SIZE 9)
"""


def _write_exec(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class CurlVersionTests(unittest.TestCase):
    def test_parse_apple_8_7_1(self):
        blob = "curl 8.7.1 (x86_64-apple-darwin23.0) libcurl/8.7.1 (SecureTransport)"
        self.assertEqual(r.parse_curl_version(blob), (8, 7, 1))
        self.assertFalse(r.supports_imap_literals((8, 7, 1)))

    def test_parse_homebrew_8_17(self):
        blob = "curl 8.17.0 (aarch64-apple-darwin24.0.0) libcurl/8.17.0"
        self.assertEqual(r.parse_curl_version(blob), (8, 17, 0))
        self.assertTrue(r.supports_imap_literals((8, 17, 0)))

    def test_eight_sixteen_still_broken(self):
        self.assertFalse(r.supports_imap_literals((8, 16, 0)))

    def test_imaps_detection(self):
        self.assertTrue(r.curl_has_imaps("Protocols: dict file http https imap imaps"))
        self.assertFalse(r.curl_has_imaps("Protocols: http https"))


class ExtractLiteralTests(unittest.TestCase):
    def test_extract_full_wrapped_fetch(self):
        size = len(RFC822.encode("latin-1"))
        wrapped = "* 1 FETCH (UID 11 BODY[] {%s}\r\n%s)\r\n" % (size, RFC822)
        self.assertEqual(r.extract_rfc822_from_fetch(wrapped), RFC822)

    def test_extract_body_peek_wrapper(self):
        size = len(RFC822.encode("latin-1"))
        wrapped = "* 1 FETCH (UID 11 BODY.PEEK[] {%s}\r\n%s)\r\n" % (size, RFC822)
        self.assertEqual(r.extract_rfc822_from_fetch(wrapped), RFC822)

    def test_extract_passthrough_raw_message(self):
        self.assertEqual(r.extract_rfc822_from_fetch(RFC822), RFC822)

    def test_extract_truncated_five_of_26973(self):
        stub = "From "
        wrapped = "* 1 FETCH (UID 96 BODY[] {26973}\r\n%s" % stub
        with self.assertRaises(r.CurlImapError) as ctx:
            r.extract_rfc822_from_fetch(wrapped)
        msg = str(ctx.exception)
        self.assertIn("got 5 of 26973", msg)
        self.assertIn("/opt/homebrew/opt/curl/bin/curl", msg)

    def test_extract_rejects_status_only(self):
        with self.assertRaises(r.CurlImapError):
            r.extract_rfc822_from_fetch("* 1 FETCH (UID 11 BODY[] {20}\r\n")

    def test_parse_size_downloads(self):
        text = (
            "=======CURL_DL_BEGIN=======42=======CURL_DL_END======="
            "=======CURL_DL_BEGIN=======26973=======CURL_DL_END======="
        )
        self.assertEqual(r.parse_size_downloads(text), [42, 26973])


class TextExtractionTests(unittest.TestCase):
    def test_plain_rfc822(self):
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

    def test_multipart_prefers_plain_skips_attachment(self):
        rec = r.record_from_rfc822(folder="INBOX", uid="12", raw=MULTIPART, meta=None)
        self.assertIn("Plain body", rec["text"])
        self.assertNotIn("%PDF", rec["text"])
        self.assertIn("HTML body", rec["html"])
        self.assertEqual(rec["subject"], "Café")

    def test_html_only_strips_tags_for_fts(self):
        rec = r.record_from_rfc822(folder="INBOX", uid="13", raw=HTML_ONLY, meta=None)
        self.assertIn("Hello", rec["text"])
        self.assertIn("world", rec["text"])
        self.assertNotIn("<p>", rec["text"])
        self.assertNotIn("alert", rec["text"])

    def test_no_raw_omits_blob(self):
        rec = r.record_from_rfc822(
            folder="INBOX", uid="11", raw=RFC822, meta=None, include_raw=False
        )
        self.assertNotIn("raw", rec)
        self.assertIn("Hi there", rec["text"])

    def test_html_to_text_entities(self):
        self.assertEqual(r.html_to_text("<p>A&amp;B</p>"), "A&B")


class ParserTests(unittest.TestCase):
    def test_parse_list_folders(self):
        folders = r.parse_list_folders(LIST_SAMPLE)
        names = [f["name"] for f in folders]
        self.assertEqual(names, ["INBOX", "Sent Messages", "Skip Me", "Archive"])
        by_name = {f["name"]: f for f in folders}
        self.assertTrue(by_name["Skip Me"]["noselect"])
        self.assertFalse(by_name["INBOX"]["noselect"])

    def test_parse_search_uids(self):
        self.assertEqual(r.parse_search_uids(SEARCH_SAMPLE), ["11", "12", "13"])
        self.assertEqual(r.parse_search_uids("* SEARCH\n"), [])

    def test_parse_fetch_meta(self):
        meta = r.parse_fetch_meta(FETCH_META_SAMPLE)
        self.assertEqual(meta["11"]["flags"], ["\\Seen"])
        self.assertEqual(meta["11"]["rfc822_size"], 123)
        self.assertEqual(meta["12"]["flags"], ["\\Flagged"])


class CurlConfigTests(unittest.TestCase):
    def test_password_only_in_config_not_argv(self):
        password = "unit-test-app-password"
        transfer = r.fetch_body_transfer("Archive", "96", mode="peek")
        config = r.build_curl_config(
            email="user@example.com",
            password=password,
            transfers=[transfer],
        )
        self.assertIn(password, config)
        self.assertIn("user = ", config)
        self.assertEqual(transfer["url"], "imaps://imap.mail.me.com:993/Archive")
        self.assertEqual(transfer["request"], "UID FETCH 96 (BODY.PEEK[])")
        argv = r.curl_argv("/opt/homebrew/opt/curl/bin/curl")
        self.assertEqual(argv[:3], ["/opt/homebrew/opt/curl/bin/curl", "-K", "-"])
        self.assertNotIn(password, argv)
        self.assertNotIn(password, " ".join(argv))

    def test_uid_url_mode_has_no_custom_fetch(self):
        transfer = r.fetch_body_transfer("Archive", "96", mode="uid-url")
        self.assertEqual(transfer["url"], "imaps://imap.mail.me.com:993/Archive;UID=96")
        self.assertNotIn("request", transfer)
        self.assertNotIn(";PEEK=", transfer["url"])

    def test_config_output_and_write_out(self):
        transfer = r.fetch_body_transfer(
            "INBOX",
            "11",
            mode="peek",
            output="/tmp/uid-11.eml",
            write_out=r.write_out_token(),
        )
        config = r.build_curl_config(
            email="user@example.com", password="x", transfers=[transfer]
        )
        self.assertIn('output = "/tmp/uid-11.eml"', config)
        self.assertIn("%{size_download}", config)
        self.assertIn("BODY.PEEK[]", config)

    def test_redact_password(self):
        self.assertEqual(r.redact("curl failed secret-value", "secret-value"), "curl failed ***")


class FindCurlTests(unittest.TestCase):
    def test_prefers_literal_capable_homebrew(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            good = _write_exec(
                tmp_path / "brew-curl",
                "#!/bin/sh\n"
                "echo 'curl 8.17.0 (aarch64-apple-darwin24.0.0)'\n"
                "echo 'Protocols: http https imap imaps'\n",
            )
            info = r.inspect_curl(str(good))
            self.assertTrue(info["literals"])
            self.assertTrue(info["imaps"])
            self.assertEqual(r.find_curl(str(good)), str(good))

    def test_require_literals_rejects_apple_8_7_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            apple = _write_exec(
                Path(tmp) / "usr-bin-curl",
                "#!/bin/sh\n"
                "echo 'curl 8.7.1 (x86_64-apple-darwin23.0)'\n"
                "echo 'Protocols: dict file ftp ftps http https imap imaps'\n",
            )
            with self.assertRaises(SystemExit) as ctx:
                r.require_literals(str(apple))
            self.assertIn("brew install curl", str(ctx.exception))
            self.assertIn("/opt/homebrew/opt/curl/bin/curl", str(ctx.exception))


FAKE_CURL = Template(
    r"""#!/usr/bin/env python3
import pathlib, re, sys
argv_path = pathlib.Path(sys.argv[0]).with_name("argv.txt")
cfg_path = pathlib.Path(sys.argv[0]).with_name("last.cfg")
if "--version" in sys.argv:
    sys.stdout.write("curl 8.17.0\nProtocols: imap imaps\n")
    raise SystemExit(0)
cfg = sys.stdin.read()
argv_path.write_text("\0".join(sys.argv))
cfg_path.write_text(cfg)
rfc = $rfc
if "BODY.PEEK[]" in cfg and "request" in cfg:
    out_m = re.search(r'output = "(.*)"', cfg)
    size = len(rfc.encode("latin-1"))
    body = "* 1 FETCH (UID 11 BODY[] {%s}\r\n%s)\r\n" % (size, rfc)
    if out_m:
        pathlib.Path(out_m.group(1)).write_bytes(body.encode("latin-1"))
    wo = re.search(r'write-out = "(.*)"', cfg)
    if wo:
        token = wo.group(1).replace("%{size_download}", str(len(body)))
        sys.stdout.write(token)
    raise SystemExit(0)
if "UID SEARCH ALL" in cfg:
    sys.stdout.write("* SEARCH 11\n")
elif "UID FETCH 1:*" in cfg:
    sys.stdout.write('* 1 FETCH (UID 11 FLAGS (\\Seen) RFC822.SIZE 20)\n')
else:
    sys.stdout.write($list)
sys.exit(0)
"""
)


class FakeCurlScriptTests(unittest.TestCase):
    def _env(self, password: str) -> dict:
        env = os.environ.copy()
        env["IMAP_APP_PASSWORD"] = password
        env["IMAP_USER"] = "user@example.com"
        return env

    def test_dry_run_does_not_fetch_body(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = _write_exec(
                tmp_path / "fake-curl",
                FAKE_CURL.substitute(rfc=repr(RFC822), list=repr(LIST_SAMPLE)),
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MAILROOM / "imap_fetch_bodies.py"),
                    "--curl-bin",
                    str(fake),
                    "--dry-run",
                    "--folder",
                    "INBOX",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self._env(password),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dry-run", proc.stdout)
            self.assertIn("uid=11", proc.stdout)
            cfg = (tmp_path / "last.cfg").read_text()
            self.assertNotIn("BODY.PEEK[]", cfg)
            self.assertNotIn(password, (tmp_path / "argv.txt").read_text())
            self.assertIn(password, cfg)

    def test_list_only(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            fake = _write_exec(
                Path(tmp) / "fake-curl",
                FAKE_CURL.substitute(rfc=repr(RFC822), list=repr(LIST_SAMPLE)),
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MAILROOM / "imap_fetch_bodies.py"),
                    "--curl-bin",
                    str(fake),
                    "--list-only",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self._env(password),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("INBOX", proc.stdout)
            self.assertIn("skip\tSkip Me", proc.stdout)

    def test_fetch_one_message_jsonl(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = _write_exec(
                tmp_path / "fake-curl",
                FAKE_CURL.substitute(rfc=repr(RFC822), list=repr(LIST_SAMPLE)),
            )
            out = tmp_path / "bodies.jsonl"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MAILROOM / "imap_fetch_bodies.py"),
                    "--curl-bin",
                    str(fake),
                    "--folder",
                    "INBOX",
                    "--max-messages",
                    "1",
                    "--overwrite",
                    "--output",
                    str(out),
                    "--no-raw",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self._env(password),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            argv_text = (tmp_path / "argv.txt").read_text()
            self.assertNotIn(password, argv_text)
            cfg = (tmp_path / "last.cfg").read_text()
            self.assertIn("BODY.PEEK[]", cfg)
            self.assertIn("output = ", cfg)
            self.assertIn("%{size_download}", cfg)
            lines = [json.loads(line) for line in out.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            rec = lines[0]
            self.assertNotIn("raw", rec)
            self.assertIn("Hi there", rec["text"])
            self.assertEqual(rec["folder"], "INBOX")
            self.assertEqual(rec["uid"], "11")

    def test_old_curl_refused_for_body_fetch(self):
        password = "unit-test-app-password"
        with tempfile.TemporaryDirectory() as tmp:
            apple = _write_exec(
                Path(tmp) / "usr-bin-curl",
                "#!/bin/sh\n"
                "echo 'curl 8.7.1 (x86_64-apple-darwin23.0)'\n"
                "echo 'Protocols: imap imaps'\n",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MAILROOM / "imap_fetch_bodies.py"),
                    "--curl-bin",
                    str(apple),
                    "--folder",
                    "INBOX",
                    "--max-messages",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self._env(password),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("brew install curl", proc.stderr)
            self.assertIn("/opt/homebrew/opt/curl/bin/curl", proc.stderr)


class NoSecretTests(unittest.TestCase):
    def test_scripts_use_placeholders_not_secrets(self):
        paths = [
            ROOT / "README.md",
            ROOT / "mailroom" / "README.md",
            MAILROOM / "imap_fetch_bodies.py",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(
                text,
                r"IMAP_APP_PASSWORD\s*=\s*['\"][^'\"]+['\"]",
            )
            self.assertNotRegex(text, r"\b\w+@me\.com\b")
            self.assertNotRegex(text, r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")

    def test_script_does_not_import_python_sockets(self):
        source = (MAILROOM / "imap_fetch_bodies.py").read_text()
        self.assertNotRegex(source, r"^\s*import imaplib\b", re.M)
        self.assertNotRegex(source, r"^\s*import socket\b", re.M)
        self.assertNotRegex(source, r"^\s*import ssl\b", re.M)
        self.assertNotRegex(source, r"^\s*from imaplib\b", re.M)
        self.assertNotIn("imap_tools", source)


if __name__ == "__main__":
    unittest.main()
