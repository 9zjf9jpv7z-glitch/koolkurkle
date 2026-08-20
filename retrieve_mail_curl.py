#!/usr/bin/env python3
"""Retrieve every iCloud IMAP message via curl imaps:// into JSONL.

Python is only an orchestrator (argparse, getpass, JSON, email parsing).
The IMAP TCP/TLS connection is opened by curl, not by Python sockets.
That is the documented Mac path: Homebrew Python on the owner's Mac
raises OSError [Errno 9] EBADF during imap-tools/imaplib connect.

Does not mark mail read (UID FETCH … BODY.PEEK[]). Does not move mail.
Never writes the app password to a file. Password comes from
IMAP_APP_PASSWORD or a getpass prompt.

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
DEFAULT_BATCH = 8

LIST_LINE = re.compile(
    r'^\* LIST \((?P<attrs>.*)\) (?P<delim>NIL|".") (?P<name>.+)\s*$'
)
SEARCH_LINE = re.compile(r"^\* SEARCH(?: (?P<uids>.*))?$", re.I)
FETCH_UID = re.compile(r"\bUID\s+(\d+)", re.I)
FETCH_SIZE = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.I)
FETCH_INTERNALDATE = re.compile(r'\bINTERNALDATE\s+"([^"]*)"', re.I)
FETCH_FLAGS = re.compile(r"\bFLAGS\s+\(([^)]*)\)", re.I)


class CurlImapError(RuntimeError):
    """curl imaps:// failed."""


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        raise SystemExit(
            "Set IMAP_APP_PASSWORD or run in a terminal so a password can be prompted."
        )
    return getpass.getpass("iCloud IMAP app password: ")


def find_curl(explicit: str | None = None) -> str:
    if explicit:
        if not (os.path.isfile(explicit) and os.access(explicit, os.X_OK)):
            raise SystemExit(f"curl binary not executable: {explicit}")
        return explicit

    candidates: list[str] = []
    env = os.environ.get("CURL_BIN")
    if env:
        candidates.append(env)
    if sys.platform == "darwin":
        candidates.extend(
            (
                "/usr/bin/curl",
                "/opt/homebrew/opt/curl/bin/curl",
                "/usr/local/opt/curl/bin/curl",
            )
        )
    which = shutil.which("curl")
    if which:
        candidates.append(which)

    seen: set[str] = set()
    errors: list[str] = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        try:
            if curl_has_imaps(path):
                return path
            errors.append(f"{path}: built without IMAP/IMAPS")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    hint = "\n".join(errors) if errors else "no curl binary found"
    raise SystemExit(
        "Need a curl binary built with IMAPS (the Mac retrieve path).\n"
        "On macOS try /usr/bin/curl or: brew install curl\n"
        f"{hint}"
    )


def curl_has_imaps(curl_bin: str) -> bool:
    result = subprocess.run(
        [curl_bin, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    return bool(re.search(r"\bimaps?\b", blob, re.I))


def escape_curl_config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_curl_config(
    *,
    email: str,
    password: str,
    transfers: list[dict],
) -> str:
    """Build a curl -K config. Password is only in this string, never argv."""
    lines = [
        "silent",
        "show-error",
        f'user = "{escape_curl_config_value(email)}:{escape_curl_config_value(password)}"',
    ]
    for index, transfer in enumerate(transfers):
        if index:
            lines.append("next")
        lines.append(f'url = "{escape_curl_config_value(transfer["url"])}"')
        request = transfer.get("request")
        if request:
            lines.append(f'request = "{escape_curl_config_value(request)}"')
        write_out = transfer.get("write_out")
        if write_out:
            lines.append(f'write-out = "{escape_curl_config_value(write_out)}"')
    lines.append("")
    return "\n".join(lines)


def curl_argv(curl_bin: str) -> list[str]:
    # Password is supplied via -K stdin, not --user on the command line.
    return [curl_bin, "-K", "-", "--connect-timeout", "30"]


def run_curl(
    curl_bin: str,
    config: str,
    *,
    password: str,
) -> str:
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
        raise CurlImapError(f"curl exited {result.returncode}: {err or 'no stderr'}")
    return stdout


def mailbox_url(mailbox: str, *, host: str = HOST, port: int = PORT) -> str:
    encoded = quote(mailbox, safe="/")
    return f"imaps://{host}:{port}/{encoded}"


def fetch_body_request(uid: str) -> str:
    if not str(uid).isdigit():
        raise ValueError(f"invalid IMAP UID: {uid!r}")
    return f"UID FETCH {uid} (BODY.PEEK[])"


def fetch_body_transfer(
    mailbox: str,
    uid: str,
    *,
    host: str = HOST,
    port: int = PORT,
    write_out: str | None = None,
) -> dict:
    """Mailbox URL + custom FETCH. macOS /usr/bin/curl rejects /;UID=;PEEK=1."""
    url = mailbox_url(mailbox, host=host, port=port)
    if "/;UID=" in url or ";PEEK=" in url:
        raise RuntimeError("internal error: fetch URL must not use /;UID= or ;PEEK=")
    transfer = {"url": url, "request": fetch_body_request(uid)}
    if write_out:
        transfer["write_out"] = write_out
    return transfer


def unquote_imap(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        inner = token[1:-1]
        out: list[str] = []
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


def parse_list_folders(text: str) -> list[dict]:
    folders: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = LIST_LINE.match(line)
        if not match:
            continue
        attrs = match.group("attrs") or ""
        delim_tok = match.group("delim")
        delim = None if delim_tok == "NIL" else unquote_imap(delim_tok)
        name = unquote_imap(match.group("name"))
        flags = [flag for flag in attrs.split() if flag]
        noselect = any(flag.lstrip("\\").lower() == "noselect" for flag in flags)
        folders.append(
            {
                "name": name,
                "delimiter": delim,
                "flags": flags,
                "noselect": noselect,
            }
        )
    return folders


def parse_search_uids(text: str) -> list[str]:
    uids: list[str] = []
    for raw_line in text.splitlines():
        match = SEARCH_LINE.match(raw_line.strip())
        if not match:
            continue
        blob = (match.group("uids") or "").strip()
        if not blob:
            continue
        uids.extend(part for part in blob.split() if part.isdigit())
    return uids


def parse_flags_token(blob: str) -> list[str]:
    return [part for part in (blob or "").split() if part]


FETCH_LITERAL = re.compile(
    r"(?is)\* \d+ FETCH \(.*?(?:BODY(?:\.PEEK)?\[\]|RFC822(?:\.PEEK)?) \{(\d+)\}\r?\n"
)


def extract_rfc822_from_fetch(text: str) -> str:
    """Pull the RFC822 bytes out of a curl UID FETCH response.

    Newer curl may emit only the message. Older/list-mode curl may emit the
    IMAP FETCH wrapper plus a {size} literal. Either is accepted. A FETCH
    status line with no literal is an error (message was not downloaded).
    """
    if text == "":
        raise CurlImapError("empty FETCH")
    match = FETCH_LITERAL.search(text)
    if match:
        size = int(match.group(1))
        start = match.end()
        raw = text[start : start + size]
        if len(raw) < size:
            raise CurlImapError(
                f"truncated FETCH literal: got {len(raw)} of {size} bytes"
            )
        return raw
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("*") and "FETCH" in first.upper():
        raise CurlImapError(
            "FETCH returned IMAP status only (no message literal). "
            "This curl may not stream BODY.PEEK[] literals."
        )
    return text


def parse_fetch_meta(text: str) -> dict[str, dict]:
    """Parse UID FETCH metadata lines (no literals expected)."""
    records: dict[str, dict] = {}
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
            "flags": parse_flags_token(flags_match.group(1) if flags_match else ""),
            "internaldate": date_match.group(1) if date_match else None,
            "rfc822_size": int(size_match.group(1)) if size_match else None,
        }
    return records


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
    parsed = getaddresses(values)
    out: list[str] = []
    for name, addr in parsed:
        label = decode_mime_header(name)
        if label and addr:
            out.append(f"{label} <{addr}>")
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
    text_parts: list[str] = []
    html_parts: list[str] = []
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


def record_from_rfc822(
    *,
    folder: str,
    uid: str,
    raw: str,
    meta: dict | None,
) -> dict:
    meta = meta or {}
    raw_bytes = raw.encode("latin-1", errors="replace")
    msg = message_from_bytes(raw_bytes)
    text, html = walk_body(msg)
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
        "rfc822_size": meta.get("rfc822_size") if meta.get("rfc822_size") is not None else len(raw_bytes),
        "raw": raw,
    }


def load_seen(path: Path) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
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


def list_folders(curl_bin: str, password: str, *, host: str = HOST, port: int = PORT) -> list[dict]:
    config = build_curl_config(
        email=EMAIL,
        password=password,
        transfers=[{"url": f"imaps://{host}:{port}/"}],
    )
    text = run_curl(curl_bin, config, password=password)
    return parse_list_folders(text)


def search_uids(
    curl_bin: str,
    password: str,
    mailbox: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> list[str]:
    config = build_curl_config(
        email=EMAIL,
        password=password,
        transfers=[
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID SEARCH ALL",
            }
        ],
    )
    text = run_curl(curl_bin, config, password=password)
    return parse_search_uids(text)


def fetch_meta(
    curl_bin: str,
    password: str,
    mailbox: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> dict[str, dict]:
    config = build_curl_config(
        email=EMAIL,
        password=password,
        transfers=[
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID FETCH 1:* (UID FLAGS INTERNALDATE RFC822.SIZE)",
            }
        ],
    )
    text = run_curl(curl_bin, config, password=password)
    return parse_fetch_meta(text)


def fetch_bodies(
    curl_bin: str,
    password: str,
    mailbox: str,
    uids: list[str],
    *,
    host: str = HOST,
    port: int = PORT,
    separator: str,
) -> list[str]:
    if not uids:
        return []
    transfers = [
        fetch_body_transfer(
            mailbox, uid, host=host, port=port, write_out=separator
        )
        for uid in uids
    ]
    config = build_curl_config(email=EMAIL, password=password, transfers=transfers)
    for line in config.splitlines():
        if line.startswith("url") and ("/;UID=" in line or ";PEEK=" in line):
            raise RuntimeError("internal error: fetch URL must not use /;UID= or ;PEEK=")
    text = run_curl(curl_bin, config, password=password)
    parts = text.split(separator)
    # curl writes write-out after each transfer; trailing split is empty.
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if len(parts) != len(uids):
        raise CurlImapError(
            f"{mailbox}: expected {len(uids)} body(ies), got {len(parts)}"
        )
    return [extract_rfc822_from_fetch(part) for part in parts]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve all iCloud IMAP mail via curl imaps:// (no Python sockets)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL path (default: {DEFAULT_OUTPUT})",
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
        help="List folders and exit. Does not write JSONL or fetch messages.",
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
        help="Stop after N new messages (0 = no limit). Useful for a smoke test.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH,
        help=f"Messages per curl --next batch (default: {DEFAULT_BATCH}).",
    )
    parser.add_argument(
        "--curl-bin",
        default=None,
        help="curl binary. Default: CURL_BIN or a curl built with IMAPS.",
    )
    parser.add_argument(
        "--host",
        default=HOST,
        help=f"IMAP host (default: {HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=PORT,
        help=f"IMAPS port (default: {PORT})",
    )
    return parser.parse_args(argv)


def retrieve(args: argparse.Namespace) -> int:
    curl_bin = find_curl(args.curl_bin)
    print(f"using curl {curl_bin} for imaps:// (Python does not open the IMAP socket)", file=sys.stderr)
    password = app_password()

    try:
        folders = list_folders(curl_bin, password, host=args.host, port=args.port)
    except CurlImapError as exc:
        raise SystemExit(f"LIST failed: {exc}") from exc

    selectable = [folder for folder in folders if not folder["noselect"]]
    if args.folders:
        wanted = set(args.folders)
        selectable = [folder for folder in selectable if folder["name"] in wanted]
        missing = wanted - {folder["name"] for folder in selectable}
        if missing:
            raise SystemExit(f"folder(s) not found or not selectable: {sorted(missing)}")

    if args.list_only:
        for folder in folders:
            mark = "skip" if folder["noselect"] else "ok"
            print(f"{mark}\t{folder['name']}")
        print(f"{len(selectable)} selectable / {len(folders)} listed", file=sys.stderr)
        return 0

    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str]] = set()
    mode = "w"
    if output.exists() and not args.overwrite:
        seen = load_seen(output)
        mode = "a"
        print(f"resume {output}: {len(seen)} message(s) already saved", file=sys.stderr)

    written = 0
    # No backslashes or quotes: curl -K write-out is a quoted config value.
    separator = f"=======CURL_IMAP_SEP_{uuid.uuid4().hex}======="
    batch_size = max(1, args.batch_size)

    with output.open(mode, encoding="utf-8") as fh:
        for folder in selectable:
            name = folder["name"]
            try:
                uids = search_uids(
                    curl_bin, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print(f"skip folder {name!r}: SEARCH failed: {exc}", file=sys.stderr)
                continue

            pending = [uid for uid in uids if (name, uid) not in seen]
            print(
                f"folder {name}: {len(uids)} uid(s), {len(pending)} new",
                file=sys.stderr,
            )
            if not pending:
                continue

            try:
                meta = fetch_meta(
                    curl_bin, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print(f"folder {name!r}: FLAGS fetch failed ({exc}); continuing", file=sys.stderr)
                meta = {}

            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                try:
                    bodies = fetch_bodies(
                        curl_bin,
                        password,
                        name,
                        chunk,
                        host=args.host,
                        port=args.port,
                        separator=separator,
                    )
                except CurlImapError:
                    bodies = []
                    for uid in chunk:
                        try:
                            bodies.extend(
                                fetch_bodies(
                                    curl_bin,
                                    password,
                                    name,
                                    [uid],
                                    host=args.host,
                                    port=args.port,
                                    separator=separator,
                                )
                            )
                        except CurlImapError as exc:
                            print(
                                f"skip {name} uid={uid}: {exc}",
                                file=sys.stderr,
                            )
                            bodies.append(None)  # type: ignore[arg-type]

                for uid, raw in zip(chunk, bodies):
                    if raw is None:
                        continue
                    rec = record_from_rfc822(
                        folder=name, uid=uid, raw=raw, meta=meta.get(uid)
                    )
                    fh.write(json.dumps(rec, ensure_ascii=False))
                    fh.write("\n")
                    fh.flush()
                    seen.add((name, uid))
                    written += 1
                    if args.max_messages and written >= args.max_messages:
                        print(
                            f"wrote {written} new message(s) to {output} (max-messages)",
                            file=sys.stderr,
                        )
                        return 0

    print(f"wrote {written} new message(s) to {output}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    print(
        "Apple /usr/bin/curl 8.7.1 can LIST folders but does not download FETCH literals.\n"
        "Use Apple Python (not ~/.venv, not Homebrew):\n"
        "  /usr/bin/python3 retrieve_mail_imaplib.py --list-only\n"
        "  /usr/bin/python3 retrieve_mail_imaplib.py --max-messages 1\n"
        "  /usr/bin/python3 retrieve_mail_imaplib.py",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
