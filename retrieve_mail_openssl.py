#!/usr/bin/env python3
"""Retrieve every iCloud IMAP message via openssl s_client into JSONL.

Python does not open the IMAP TCP/TLS socket. /usr/bin/openssl does.
That matters in the owner's zsh: Apple Python IMAP4_SSL died with
OSError [Errno 9] EBADF in sock.connect, before LOGIN. Apple curl in
that same Terminal listed 33 folders but did not stream FETCH literals.

Copy this file to the Mac (Desktop is fine) and run:

  /usr/bin/python3 ~/Desktop/retrieve_mail_openssl.py --list-only
  /usr/bin/python3 ~/Desktop/retrieve_mail_openssl.py --max-messages 1

Standalone: no repo imports. Password from IMAP_APP_PASSWORD or getpass;
never written to a file; not placed on openssl argv. EXAMINE + BODY.PEEK[]
so mail is not marked read. Mail is not moved.

Default output: ~/Desktop/icloud_mail_all.jsonl

Login/LIST/FETCH over openssl have not been run in the owner's Terminal.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

HOST = "imap.mail.me.com"
PORT = 993
EMAIL = "kirkbacon@me.com"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "icloud_mail_all.jsonl"
FETCH_ITEMS = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
DEFAULT_TIMEOUT = 60

LIST_ITEM = re.compile(
    r'^(?:\* LIST )?\((?P<attrs>.*)\) (?P<delim>NIL|".") (?P<name>.+)\s*$'
)
FETCH_UID = re.compile(r"\bUID\s+(\d+)", re.I)
FETCH_SIZE = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.I)
FETCH_INTERNALDATE = re.compile(r'\bINTERNALDATE\s+"([^"]*)"', re.I)
FETCH_FLAGS = re.compile(r"\bFLAGS\s+\(([^)]*)\)", re.I)
LITERAL_END = re.compile(br"\{(\d+)\}\r?\n$")


class ImapError(RuntimeError):
    """IMAP over openssl failed."""


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        raise SystemExit(
            "Set IMAP_APP_PASSWORD or run in a terminal so a password can be prompted."
        )
    return getpass.getpass("iCloud IMAP app password: ")


def find_openssl(explicit=None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("OPENSSL_BIN")
    if env:
        candidates.append(env)
    if sys.platform == "darwin":
        candidates.append("/usr/bin/openssl")
    which = shutil.which("openssl")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise SystemExit("Need /usr/bin/openssl (or OPENSSL_BIN) for IMAP TLS.")


def imap_quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_mailbox(name: str) -> str:
    if name.upper() == "INBOX":
        return "INBOX"
    if re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return name
    return imap_quoted(name)


def unquote_imap(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        inner = token[1:-1]
        out = []
        escaped = False
        for char in inner:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        return "".join(out)
    return token


def parse_list_item(item) -> dict | None:
    if item is None:
        return None
    if isinstance(item, (bytes, bytearray)):
        text = item.decode("utf-8", "replace")
    else:
        text = str(item)
    text = text.strip()
    match = LIST_ITEM.match(text)
    if not match:
        return None
    flags = [part for part in (match.group("attrs") or "").split() if part]
    delim_tok = match.group("delim")
    return {
        "name": unquote_imap(match.group("name")),
        "delimiter": None if delim_tok == "NIL" else unquote_imap(delim_tok),
        "flags": flags,
        "noselect": any(flag.lstrip("\\").lower() == "noselect" for flag in flags),
    }


def parse_list_folders(items) -> list:
    folders = []
    for item in items or []:
        parsed = parse_list_item(item)
        if parsed:
            folders.append(parsed)
    return folders


def parse_search_uids(data) -> list:
    uids = []
    for item in data or []:
        if item is None:
            continue
        text = item.decode("ascii", "replace") if isinstance(item, (bytes, bytearray)) else str(item)
        if text.upper().startswith("* SEARCH"):
            text = text[8:]
        uids.extend(part for part in text.split() if part.isdigit())
    return uids


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def addresses_from(msg: Message, header: str) -> list:
    values = msg.get_all(header, [])
    if not values:
        return []
    out = []
    for name, addr in getaddresses(values):
        label = decode_mime_header(name)
        if label and addr:
            out.append("%s <%s>" % (label, addr))
        elif addr:
            out.append(addr)
        elif label:
            out.append(label)
    return out


def first_from(msg: Message) -> str:
    addrs = addresses_from(msg, "From")
    return addrs[0] if addrs else decode_mime_header(msg.get("From", ""))


def date_iso(msg: Message, internaldate: str | None) -> str | None:
    raw = msg.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    if internaldate:
        for fmt in ("%d-%b-%Y %H:%M:%S %z", " %d-%b-%Y %H:%M:%S %z"):
            try:
                return datetime.strptime(internaldate, fmt).isoformat()
            except ValueError:
                continue
    return None


def walk_body(msg: Message) -> tuple:
    text_parts = []
    html_parts = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if disposition.startswith("attachment"):
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            html_parts.append(decoded)
        elif ctype == "text/plain":
            text_parts.append(decoded)
    return "".join(text_parts), "".join(html_parts)


def record_from_rfc822(folder, uid, raw, meta) -> dict:
    meta = meta or {}
    raw_bytes = raw.encode("latin-1", errors="replace")
    msg = message_from_bytes(raw_bytes)
    text, html = walk_body(msg)
    size = meta.get("rfc822_size")
    if size is None:
        size = len(raw_bytes)
    return {
        "folder": folder,
        "uid": str(uid),
        "flags": list(meta.get("flags") or []),
        "internaldate": meta.get("internaldate"),
        "date": date_iso(msg, meta.get("internaldate")),
        "from": first_from(msg),
        "to": addresses_from(msg, "To"),
        "cc": addresses_from(msg, "Cc"),
        "subject": decode_mime_header(msg.get("Subject")),
        "message_id": (msg.get("Message-ID") or "").strip(),
        "text": text,
        "html": html,
        "rfc822_size": size,
        "raw": raw,
    }


def load_seen(path: Path) -> set:
    seen = set()
    if not path.is_file():
        return seen
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            folder = rec.get("folder")
            uid = rec.get("uid")
            if folder is not None and uid is not None:
                seen.add((str(folder), str(uid)))
    return seen


def openssl_argv(openssl_bin: str, host: str, port: int) -> list:
    # Password is not on this argv. LOGIN goes on openssl stdin after TLS.
    return [
        openssl_bin,
        "s_client",
        "-connect",
        "%s:%s" % (host, port),
        "-servername",
        host,
        "-quiet",
        "-ign_eof",
    ]


class OpensslImap:
    """IMAP client whose TCP/TLS socket is openssl, not Python."""

    def __init__(self, openssl_bin: str, host: str, port: int, timeout: float = DEFAULT_TIMEOUT):
        argv = openssl_argv(openssl_bin, host, port)
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.timeout = timeout
        self._buf = b""
        self._tag = 0
        self._stderr = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        if self.proc.stdin is None or self.proc.stdout is None:
            raise ImapError("openssl pipes missing")

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr.append(chunk)
                if sum(len(part) for part in self._stderr) > 65536:
                    self._stderr = self._stderr[-4:]
        except Exception:
            return

    def _stderr_text(self) -> str:
        return b"".join(self._stderr).decode("latin-1", "replace")[-2000:]

    def _fill(self):
        if self.proc.poll() is not None and not self._buf:
            err = self._stderr_text().strip()
            raise ImapError(
                "openssl exited %s%s"
                % (self.proc.returncode, (": " + err) if err else "")
            )
        stdout = self.proc.stdout
        ready, _, _ = select.select([stdout], [], [], self.timeout)
        if not ready:
            raise ImapError("timeout waiting for IMAP data from openssl")
        chunk = stdout.read(65536)
        if not chunk:
            if self.proc.poll() is not None:
                err = self._stderr_text().strip()
                raise ImapError(
                    "openssl EOF%s" % ((": " + err) if err else "")
                )
            return
        self._buf += chunk

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            self._fill()
        index = self._buf.index(b"\n") + 1
        line, self._buf = self._buf[:index], self._buf[index:]
        return line

    def read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._fill()
        out, self._buf = self._buf[:size], self._buf[size:]
        if len(out) < size:
            raise ImapError("truncated IMAP literal: got %s of %s bytes" % (len(out), size))
        return out

    def wait_greeting(self):
        deadline_lines = 200
        for _ in range(deadline_lines):
            line = self.readline()
            if line.startswith(b"* OK"):
                return
        raise ImapError("no IMAP greeting from openssl s_client")

    def _next_tag(self) -> str:
        self._tag += 1
        return "A%04d" % self._tag

    def send_line(self, line: str):
        self.proc.stdin.write((line + "\r\n").encode("utf-8"))
        self.proc.stdin.flush()

    def command(self, payload: str, *, secret=False) -> tuple:
        """Run one tagged command. Returns (status, untagged_text_lines, literals)."""
        tag = self._next_tag()
        self.send_line("%s %s" % (tag, payload))
        tag_b = tag.encode("ascii") + b" "
        untagged = []
        literals = []
        while True:
            line = self.readline()
            match = LITERAL_END.search(line)
            if match:
                size = int(match.group(1))
                literals.append(self.read_exact(size))
                untagged.append(line.decode("latin-1", "replace"))
                continue
            if line.startswith(tag_b):
                parts = line.decode("latin-1", "replace").split(None, 2)
                status = parts[1] if len(parts) > 1 else "BAD"
                if status != "OK":
                    detail = line.decode("latin-1", "replace").strip()
                    if secret:
                        detail = tag + " " + status + " (redacted)"
                    raise ImapError(detail)
                return status, untagged, literals
            untagged.append(line.decode("latin-1", "replace"))

    def login(self, email: str, password: str):
        self.command(
            "LOGIN %s %s" % (imap_quoted(email), imap_quoted(password)),
            secret=True,
        )

    def list_folders(self) -> list:
        _status, untagged, _lits = self.command('LIST "" "*"')
        return parse_list_folders(untagged)

    def examine(self, mailbox: str):
        self.command("EXAMINE %s" % quote_mailbox(mailbox))

    def uid_search_all(self) -> list:
        _status, untagged, _lits = self.command("UID SEARCH ALL")
        return parse_search_uids(untagged)

    def uid_fetch(self, uid: str) -> tuple:
        if not str(uid).isdigit():
            raise ImapError("invalid UID %r" % uid)
        _status, untagged, literals = self.command(
            "UID FETCH %s %s" % (uid, FETCH_ITEMS)
        )
        if not literals:
            preview = " ".join(part.strip() for part in untagged)[:240]
            raise ImapError(
                "FETCH uid=%s had no literal (untagged: %s)" % (uid, preview)
            )
        raw = literals[0]
        if len(literals) > 1:
            raw = max(literals, key=len)
        if len(raw) == 0:
            raise ImapError("FETCH uid=%s literal was 0 bytes" % uid)
        header = " ".join(untagged)
        uid_match = FETCH_UID.search(header)
        flags_match = FETCH_FLAGS.search(header)
        date_match = FETCH_INTERNALDATE.search(header)
        size_match = FETCH_SIZE.search(header)
        meta = {
            "uid": uid_match.group(1) if uid_match else str(uid),
            "flags": [part for part in (flags_match.group(1) if flags_match else "").split() if part],
            "internaldate": date_match.group(1) if date_match else None,
            "rfc822_size": int(size_match.group(1)) if size_match else len(raw),
        }
        return raw.decode("latin-1"), meta

    def logout(self):
        try:
            self.command("LOGOUT")
        except Exception:
            pass
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve iCloud IMAP mail via openssl s_client (not Python sockets)."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--folder", action="append", dest="folders")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-messages", type=int, default=0)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--openssl-bin", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser.parse_args(argv)


def retrieve(args: argparse.Namespace) -> int:
    openssl_bin = find_openssl(args.openssl_bin)
    print(
        "using %s s_client for IMAP TLS (Python does not connect to %s)"
        % (openssl_bin, args.host),
        file=sys.stderr,
    )
    password = app_password()
    conn = OpensslImap(openssl_bin, args.host, args.port, timeout=args.timeout)
    try:
        conn.wait_greeting()
        conn.login(EMAIL, password)
        folders = conn.list_folders()
        selectable = [folder for folder in folders if not folder["noselect"]]
        if args.folders:
            wanted = set(args.folders)
            selectable = [folder for folder in selectable if folder["name"] in wanted]
            missing = wanted - {folder["name"] for folder in selectable}
            if missing:
                raise SystemExit("folder(s) not found or not selectable: %s" % sorted(missing))

        if args.list_only:
            for folder in folders:
                mark = "skip" if folder["noselect"] else "ok"
                print("%s\t%s" % (mark, folder["name"]))
            print(
                "%s selectable / %s listed" % (len(selectable), len(folders)),
                file=sys.stderr,
            )
            return 0

        output = args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        seen = set()
        mode = "w"
        if output.exists() and not args.overwrite:
            seen = load_seen(output)
            mode = "a"
            print(
                "resume %s: %s message(s) already saved" % (output, len(seen)),
                file=sys.stderr,
            )

        written = 0
        with output.open(mode, encoding="utf-8") as fh:
            for folder in selectable:
                name = folder["name"]
                try:
                    conn.examine(name)
                    uids = conn.uid_search_all()
                except ImapError as exc:
                    print("skip folder %r: %s" % (name, exc), file=sys.stderr)
                    continue
                pending = [uid for uid in uids if (name, uid) not in seen]
                print(
                    "folder %s: %s uid(s), %s new" % (name, len(uids), len(pending)),
                    file=sys.stderr,
                )
                for uid in pending:
                    try:
                        raw, meta = conn.uid_fetch(uid)
                    except ImapError as exc:
                        print("skip %s uid=%s: %s" % (name, uid, exc), file=sys.stderr)
                        continue
                    rec = record_from_rfc822(name, uid, raw, meta)
                    if not rec.get("raw"):
                        print("skip %s uid=%s: empty raw" % (name, uid), file=sys.stderr)
                        continue
                    fh.write(json.dumps(rec, ensure_ascii=False))
                    fh.write("\n")
                    fh.flush()
                    seen.add((name, uid))
                    written += 1
                    if args.max_messages and written >= args.max_messages:
                        print(
                            "wrote %s new message(s) to %s (max-messages)"
                            % (written, output),
                            file=sys.stderr,
                        )
                        return 0
        print("wrote %s new message(s) to %s" % (written, output), file=sys.stderr)
        return 0
    finally:
        conn.logout()


def main(argv=None) -> int:
    return retrieve(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
