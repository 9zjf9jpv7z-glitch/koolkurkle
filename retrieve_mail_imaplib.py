#!/usr/bin/env python3
"""imaplib retrieve — failed in the owner's zsh.

/usr/bin/python3 ~/Desktop/retrieve_mail_imaplib.py --list-only died in
IMAP4_SSL → socket.create_connection → OSError [Errno 9] EBADF before LOGIN.
An earlier create_connection success was another agent process, not that
Terminal. Use retrieve_mail_openssl.py instead.
"""

from __future__ import annotations

import argparse
import getpass
import imaplib
import json
import os
import re
import ssl
import sys
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

LIST_ITEM = re.compile(
    r'^(?:\* LIST )?\((?P<attrs>.*)\) (?P<delim>NIL|".") (?P<name>.+)\s*$'
)
FETCH_UID = re.compile(r"\bUID\s+(\d+)", re.I)
FETCH_SIZE = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.I)
FETCH_INTERNALDATE = re.compile(r'\bINTERNALDATE\s+"([^"]*)"', re.I)
FETCH_FLAGS = re.compile(r"\bFLAGS\s+\(([^)]*)\)", re.I)


class ImapError(RuntimeError):
    """IMAP command failed or returned no message literal."""


def reject_broken_mac_python() -> None:
    """Homebrew / venv Python on this Mac cannot open IMAP sockets."""
    if sys.platform != "darwin":
        return
    exe = os.path.realpath(sys.executable)
    markers = ("homebrew", "Cellar", ".venv", "/opt/homebrew/")
    if any(marker in exe for marker in markers):
        raise SystemExit(
            f"{exe} cannot open IMAP sockets on this Mac (EBADF).\n"
            "Use: /usr/bin/python3 retrieve_mail_imaplib.py"
        )


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        raise SystemExit(
            "Set IMAP_APP_PASSWORD or run in a terminal so a password can be prompted."
        )
    return getpass.getpass("iCloud IMAP app password: ")


def quote_mailbox(name: str) -> str:
    if name.upper() == "INBOX":
        return "INBOX"
    if re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


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


def parse_list_folders(items) -> list[dict]:
    folders = []
    for item in items or []:
        parsed = parse_list_item(item)
        if parsed:
            folders.append(parsed)
    return folders


def parse_search_uids(data) -> list[str]:
    uids = []
    for item in data or []:
        if item is None:
            continue
        text = item.decode("ascii", "replace") if isinstance(item, (bytes, bytearray)) else str(item)
        uids.extend(part for part in text.split() if part.isdigit())
    return uids


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("latin-1", "replace")
    return str(value)


def parse_imaplib_fetch(data) -> tuple[str, dict]:
    """Return (rfc822_latin1, meta) from imaplib UID FETCH data.

    imaplib delivers literals as the second element of a tuple. A FETCH
    status line with no tuple body is not a downloaded message.
    """
    header_parts = []
    raw = None
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple) and len(item) >= 2:
            header_parts.append(_as_text(item[0]))
            if isinstance(item[1], (bytes, bytearray)):
                raw = bytes(item[1])
            elif item[1] is not None:
                raw = _as_text(item[1]).encode("latin-1", "replace")
        else:
            header_parts.append(_as_text(item))
    header = " ".join(header_parts)
    if raw is None:
        raise ImapError("FETCH had no RFC822 literal (status line only)")
    if len(raw) == 0:
        raise ImapError("FETCH literal was 0 bytes")
    uid_match = FETCH_UID.search(header)
    flags_match = FETCH_FLAGS.search(header)
    date_match = FETCH_INTERNALDATE.search(header)
    size_match = FETCH_SIZE.search(header)
    meta = {
        "uid": uid_match.group(1) if uid_match else None,
        "flags": [part for part in (flags_match.group(1) if flags_match else "").split() if part],
        "internaldate": date_match.group(1) if date_match else None,
        "rfc822_size": int(size_match.group(1)) if size_match else len(raw),
    }
    return raw.decode("latin-1"), meta


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def addresses_from(msg: Message, header: str) -> list[str]:
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


def walk_body(msg: Message) -> tuple[str, str]:
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


def record_from_rfc822(folder: str, uid: str, raw: str, meta: dict | None) -> dict:
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


def connect(password: str, host: str = HOST, port: int = PORT) -> imaplib.IMAP4_SSL:
    context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=60)
    typ, _ = conn.login(EMAIL, password)
    if typ != "OK":
        raise ImapError("LOGIN failed: %s" % typ)
    return conn


def list_folders(conn: imaplib.IMAP4_SSL) -> list[dict]:
    typ, data = conn.list()
    if typ != "OK":
        raise ImapError("LIST failed: %s" % typ)
    return parse_list_folders(data)


def search_uids(conn: imaplib.IMAP4_SSL, mailbox: str) -> list[str]:
    quoted = quote_mailbox(mailbox)
    typ, _ = conn.select(quoted, readonly=True)
    if typ != "OK":
        raise ImapError("EXAMINE/select readonly failed for %r" % mailbox)
    typ, data = conn.uid("SEARCH", None, "ALL")
    if typ != "OK":
        raise ImapError("UID SEARCH failed for %r" % mailbox)
    return parse_search_uids(data)


def fetch_one(conn: imaplib.IMAP4_SSL, uid: str) -> tuple[str, dict]:
    if not str(uid).isdigit():
        raise ImapError("invalid UID %r" % uid)
    typ, data = conn.uid("FETCH", uid, FETCH_ITEMS)
    if typ != "OK":
        raise ImapError("UID FETCH %s failed: %s" % (uid, typ))
    raw, meta = parse_imaplib_fetch(data)
    if not meta.get("uid"):
        meta["uid"] = str(uid)
    return raw, meta


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve all iCloud IMAP mail via stdlib imaplib (Apple /usr/bin/python3)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSONL path (default: %s)" % DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--folder",
        action="append",
        dest="folders",
        help="Limit to this folder (repeatable). Default: every selectable folder.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List folders and exit. Remaining Mac test: login + LIST.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the output file instead of resuming (skip existing folder+uid).",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Stop after N new messages (0 = no limit).",
    )
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    return parser.parse_args(argv)


def retrieve(args: argparse.Namespace) -> int:
    reject_broken_mac_python()
    print(
        "using %s + stdlib imaplib (EXAMINE, BODY.PEEK[])" % sys.executable,
        file=sys.stderr,
    )
    if sys.platform == "darwin" and "usr/bin/python" not in os.path.realpath(sys.executable):
        print(
            "note: on the owner's Mac run this with /usr/bin/python3, not ~/.venv",
            file=sys.stderr,
        )
    password = app_password()
    conn = connect(password, host=args.host, port=args.port)
    try:
        folders = list_folders(conn)
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
                    uids = search_uids(conn, name)
                except (ImapError, imaplib.IMAP4.error) as exc:
                    print("skip folder %r: %s" % (name, exc), file=sys.stderr)
                    continue
                pending = [uid for uid in uids if (name, uid) not in seen]
                print(
                    "folder %s: %s uid(s), %s new" % (name, len(uids), len(pending)),
                    file=sys.stderr,
                )
                for uid in pending:
                    try:
                        raw, meta = fetch_one(conn, uid)
                    except (ImapError, imaplib.IMAP4.error) as exc:
                        print("skip %s uid=%s: %s" % (name, uid, exc), file=sys.stderr)
                        continue
                    rec = record_from_rfc822(name, uid, raw, meta)
                    if not rec.get("raw"):
                        print(
                            "skip %s uid=%s: empty raw after parse" % (name, uid),
                            file=sys.stderr,
                        )
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
        try:
            conn.logout()
        except Exception:
            pass


def main(argv=None) -> int:
    return retrieve(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
