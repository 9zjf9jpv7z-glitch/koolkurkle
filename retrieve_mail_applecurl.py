#!/usr/bin/env python3
"""Retrieve iCloud mail using Apple /usr/bin/curl — the only IMAP that
worked in the owner's zsh.

Owner zsh tonight:
  curl LIST: 33 folders (app password worked).
  curl custom UID FETCH (BODY.PEEK[]): {size} then 0 body bytes.
  curl URL ;PEEK=1: curl (3) illegal URL.
  Python IMAP4_SSL: EBADF before LOGIN.
  openssl s_client via retrieve_mail_openssl.py: connect errno 9.

Python does not connect to IMAP. /usr/bin/curl does. LIST/SEARCH stay on
the mailbox URL + custom request (that path listed folders). Message
bodies use curl's native IMAP URL with ;UID= and without ;PEEK=1 — the
FETCH state that writes RFC822 to stdout. That URL has not been run in
their zsh yet.

Password: IMAP_APP_PASSWORD or getpass. Never a file. Never curl argv
(--user goes in curl -K stdin only).

Native curl fetch uses BODY[] and may set \\Seen. FLAGS are read first
(line-based FETCH, no literal). If the message was unseen, this script
sends UID STORE -FLAGS.SILENT (\\Seen) after a successful download.
Mail is not moved.

Copy to Desktop and run in THEIR zsh:

  /usr/bin/python3 ~/Desktop/retrieve_mail_applecurl.py --list-only
  /usr/bin/python3 ~/Desktop/retrieve_mail_applecurl.py --max-messages 1

Default output: ~/Desktop/icloud_mail_all.jsonl
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

HOST = "imap.mail.me.com"
PORT = 993
EMAIL = "kirkbacon@me.com"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "icloud_mail_all.jsonl"
DEFAULT_BATCH = 4

LIST_ITEM = re.compile(
    r'^(?:\* LIST )?\((?P<attrs>.*)\) (?P<delim>NIL|".") (?P<name>.+)\s*$'
)
SEARCH_LINE = re.compile(r"^\* SEARCH(?: (?P<uids>.*))?$", re.I)
FETCH_UID = re.compile(r"\bUID\s+(\d+)", re.I)
FETCH_SIZE = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.I)
FETCH_INTERNALDATE = re.compile(r'\bINTERNALDATE\s+"([^"]*)"', re.I)
FETCH_FLAGS = re.compile(r"\bFLAGS\s+\(([^)]*)\)", re.I)
FETCH_LITERAL = re.compile(
    r"(?is)\* \d+ FETCH \(.*?(?:BODY(?:\.PEEK)?\[\]|RFC822(?:\.PEEK)?) \{(\d+)\}\r?\n"
)


class CurlImapError(RuntimeError):
    """curl IMAP failed."""


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        raise SystemExit(
            "Set IMAP_APP_PASSWORD or run in a terminal so a password can be prompted."
        )
    return getpass.getpass("iCloud IMAP app password: ")


def find_curl(explicit=None) -> str:
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise SystemExit("curl binary not executable: %s" % explicit)
    env = os.environ.get("CURL_BIN")
    candidates = []
    if env:
        candidates.append(env)
    if sys.platform == "darwin":
        candidates.append("/usr/bin/curl")
    which = shutil.which("curl")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise SystemExit("Need /usr/bin/curl (the binary that already LISTed folders).")


def escape_curl_config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_curl_config(email: str, password: str, transfers: list) -> str:
    lines = [
        "silent",
        "show-error",
        'user = "%s:%s"'
        % (escape_curl_config_value(email), escape_curl_config_value(password)),
    ]
    for index, transfer in enumerate(transfers):
        if index:
            lines.append("next")
        lines.append('url = "%s"' % escape_curl_config_value(transfer["url"]))
        if transfer.get("request"):
            lines.append(
                'request = "%s"' % escape_curl_config_value(transfer["request"])
            )
        if transfer.get("write_out"):
            lines.append(
                'write-out = "%s"' % escape_curl_config_value(transfer["write_out"])
            )
    lines.append("")
    return "\n".join(lines)


def curl_argv(curl_bin: str) -> list:
    return [curl_bin, "-K", "-", "--connect-timeout", "30"]


def run_curl(curl_bin: str, config: str, password: str) -> str:
    argv = curl_argv(curl_bin)
    if password and password in argv:
        raise RuntimeError("internal error: password leaked into curl argv")
    result = subprocess.run(
        argv,
        input=config.encode("utf-8"),
        check=False,
        capture_output=True,
    )
    stdout = (result.stdout or b"").decode("latin-1")
    stderr = (result.stderr or b"").decode("latin-1", errors="replace")
    if result.returncode != 0:
        err = (stderr or stdout).strip()
        raise CurlImapError("curl exited %s: %s" % (result.returncode, err or "no stderr"))
    return stdout


def mailbox_url(mailbox: str, host=HOST, port=PORT) -> str:
    return "imaps://%s:%s/%s" % (host, port, quote(mailbox, safe="/"))


def message_url(mailbox: str, uid: str, style: str, host=HOST, port=PORT) -> str:
    """Native curl IMAP fetch URL. No ;PEEK=1 (that was curl 3 in their zsh)."""
    if not str(uid).isdigit():
        raise ValueError("invalid UID %r" % uid)
    base = mailbox_url(mailbox, host=host, port=port)
    if style == "noslash":
        url = "%s;UID=%s" % (base, uid)
    else:
        url = "%s/;UID=%s" % (base, uid)
    if ";PEEK=" in url:
        raise RuntimeError("internal error: ;PEEK= must not appear in the fetch URL")
    return url


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


def parse_list_folders(text: str) -> list:
    folders = []
    for raw_line in text.splitlines():
        match = LIST_ITEM.match(raw_line.strip())
        if not match:
            continue
        flags = [part for part in (match.group("attrs") or "").split() if part]
        delim_tok = match.group("delim")
        folders.append(
            {
                "name": unquote_imap(match.group("name")),
                "delimiter": None if delim_tok == "NIL" else unquote_imap(delim_tok),
                "flags": flags,
                "noselect": any(
                    flag.lstrip("\\").lower() == "noselect" for flag in flags
                ),
            }
        )
    return folders


def parse_search_uids(text: str) -> list:
    uids = []
    for raw_line in text.splitlines():
        match = SEARCH_LINE.match(raw_line.strip())
        if not match:
            continue
        blob = (match.group("uids") or "").strip()
        uids.extend(part for part in blob.split() if part.isdigit())
    return uids


def parse_fetch_meta(text: str) -> dict:
    records = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "FETCH" not in line.upper():
            continue
        uid_match = FETCH_UID.search(line)
        if not uid_match:
            continue
        uid = uid_match.group(1)
        flags_match = FETCH_FLAGS.search(line)
        date_match = FETCH_INTERNALDATE.search(line)
        size_match = FETCH_SIZE.search(line)
        records[uid] = {
            "uid": uid,
            "flags": [part for part in (flags_match.group(1) if flags_match else "").split() if part],
            "internaldate": date_match.group(1) if date_match else None,
            "rfc822_size": int(size_match.group(1)) if size_match else None,
        }
    return records


def has_seen(flags) -> bool:
    return any(str(flag).lstrip("\\").lower() == "seen" for flag in (flags or []))


def extract_rfc822(text: str) -> str:
    """Native curl UID fetch should be raw RFC822. A {size} line with no
    following bytes is the failed custom-FETCH mode — not a message."""
    if text == "":
        raise CurlImapError("empty FETCH")
    match = FETCH_LITERAL.search(text)
    if match:
        size = int(match.group(1))
        raw = text[match.end() : match.end() + size]
        if len(raw) < size:
            raise CurlImapError(
                "truncated FETCH literal: got %s of %s bytes (custom -X path; not native UID URL)"
                % (len(raw), size)
            )
        return raw
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("*") and "FETCH" in first.upper():
        raise CurlImapError("FETCH status line only; no RFC822 body")
    return text


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


def list_folders(curl_bin, password, host=HOST, port=PORT) -> list:
    config = build_curl_config(
        EMAIL, password, [{"url": "imaps://%s:%s/" % (host, port)}]
    )
    return parse_list_folders(run_curl(curl_bin, config, password))


def search_uids(curl_bin, password, mailbox, host=HOST, port=PORT) -> list:
    config = build_curl_config(
        EMAIL,
        password,
        [
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID SEARCH ALL",
            }
        ],
    )
    return parse_search_uids(run_curl(curl_bin, config, password))


def fetch_meta(curl_bin, password, mailbox, host=HOST, port=PORT) -> dict:
    config = build_curl_config(
        EMAIL,
        password,
        [
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID FETCH 1:* (UID FLAGS INTERNALDATE RFC822.SIZE)",
            }
        ],
    )
    return parse_fetch_meta(run_curl(curl_bin, config, password))


def _split_bodies(text: str, separator: str, count: int) -> list:
    parts = text.split(separator)
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) != count:
        raise CurlImapError("expected %s body(ies), got %s" % (count, len(parts)))
    return [extract_rfc822(part) for part in parts]


def fetch_bodies(
    curl_bin,
    password,
    mailbox,
    uids,
    style,
    host=HOST,
    port=PORT,
    separator="",
) -> tuple:
    """Return (bodies, style_used). style is slash or noslash."""
    if not uids:
        return [], style
    styles = [style] if style else ["slash", "noslash"]
    last_error = None
    for candidate in styles:
        transfers = [
            {
                "url": message_url(mailbox, uid, candidate, host=host, port=port),
                "write_out": separator,
            }
            for uid in uids
        ]
        for transfer in transfers:
            if ";PEEK=" in transfer["url"]:
                raise RuntimeError("internal error: ;PEEK= in fetch URL")
            if transfer.get("request"):
                raise RuntimeError("internal error: body fetch must not use -X")
        config = build_curl_config(EMAIL, password, transfers)
        try:
            text = run_curl(curl_bin, config, password)
            return _split_bodies(text, separator, len(uids)), candidate
        except CurlImapError as exc:
            last_error = exc
            err = str(exc)
            if "exited 3" not in err and "bad/illegal format" not in err.lower():
                raise
            continue
    raise last_error or CurlImapError("UID URL fetch failed")


def clear_seen(curl_bin, password, mailbox, uid, host=HOST, port=PORT) -> None:
    config = build_curl_config(
        EMAIL,
        password,
        [
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID STORE %s -FLAGS.SILENT (\\Seen)" % uid,
            }
        ],
    )
    run_curl(curl_bin, config, password)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve iCloud mail via Apple curl native IMAP UID URLs."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--folder", action="append", dest="folders")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-messages", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--curl-bin", default=None)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--no-restore-seen",
        action="store_true",
        help="Do not UID STORE -FLAGS (\\Seen) after native BODY[] fetch.",
    )
    return parser.parse_args(argv)


def retrieve(args: argparse.Namespace) -> int:
    curl_bin = find_curl(args.curl_bin)
    print(
        "using %s for IMAP (native /;UID=n fetch, no ;PEEK=1, no custom BODY.PEEK -X)"
        % curl_bin,
        file=sys.stderr,
    )
    password = app_password()
    folders = list_folders(curl_bin, password, host=args.host, port=args.port)
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
        print("resume %s: %s message(s) already saved" % (output, len(seen)), file=sys.stderr)

    written = 0
    separator = "=======CURL_IMAP_SEP_%s=======" % uuid.uuid4().hex
    batch_size = max(1, args.batch_size)
    url_style = "slash"

    with output.open(mode, encoding="utf-8") as fh:
        for folder in selectable:
            name = folder["name"]
            try:
                uids = search_uids(
                    curl_bin, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print("skip folder %r: SEARCH failed: %s" % (name, exc), file=sys.stderr)
                continue
            pending = [uid for uid in uids if (name, uid) not in seen]
            print(
                "folder %s: %s uid(s), %s new" % (name, len(uids), len(pending)),
                file=sys.stderr,
            )
            if not pending:
                continue
            try:
                meta = fetch_meta(
                    curl_bin, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print("folder %r: FLAGS fetch failed (%s)" % (name, exc), file=sys.stderr)
                meta = {}

            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                try:
                    bodies, url_style = fetch_bodies(
                        curl_bin,
                        password,
                        name,
                        chunk,
                        url_style,
                        host=args.host,
                        port=args.port,
                        separator=separator,
                    )
                except CurlImapError:
                    bodies = []
                    for uid in chunk:
                        try:
                            one, url_style = fetch_bodies(
                                curl_bin,
                                password,
                                name,
                                [uid],
                                url_style,
                                host=args.host,
                                port=args.port,
                                separator=separator,
                            )
                            bodies.extend(one)
                        except CurlImapError as exc:
                            print("skip %s uid=%s: %s" % (name, uid, exc), file=sys.stderr)
                            bodies.append(None)

                for uid, raw in zip(chunk, bodies):
                    if raw is None:
                        continue
                    rec = record_from_rfc822(name, uid, raw, meta.get(uid))
                    if not rec.get("raw"):
                        print("skip %s uid=%s: empty raw" % (name, uid), file=sys.stderr)
                        continue
                    fh.write(json.dumps(rec, ensure_ascii=False))
                    fh.write("\n")
                    fh.flush()
                    if (
                        not args.no_restore_seen
                        and meta.get(uid) is not None
                        and not has_seen(meta[uid].get("flags"))
                    ):
                        try:
                            clear_seen(
                                curl_bin,
                                password,
                                name,
                                uid,
                                host=args.host,
                                port=args.port,
                            )
                        except CurlImapError as exc:
                            print(
                                "warn %s uid=%s: could not clear \\Seen: %s"
                                % (name, uid, exc),
                                file=sys.stderr,
                            )
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


def main(argv=None) -> int:
    return retrieve(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
