#!/usr/bin/env python3
"""Fetch full iCloud IMAP messages via curl BODY.PEEK[] (no Python sockets).

Apple /usr/bin/curl 8.7.1 truncates IMAP {size} literals on custom
UID FETCH (curl#18847) — e.g. 5 of 26973 bytes. Homebrew curl 8.17.0+
streams the literal. Point CURL_BIN at:

  /opt/homebrew/opt/curl/bin/curl          # Apple Silicon
  /usr/local/opt/curl/bin/curl             # Intel
  $(brew --prefix curl)/bin/curl

Password: IMAP_APP_PASSWORD or getpass. Never written to a file. Never
placed on curl argv (--user goes in curl -K stdin only).

IMAP username: --user or IMAP_USER (no hardcoded address).
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
import tempfile
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote

HOST = "imap.mail.me.com"
PORT = 993
DEFAULT_OUTPUT = Path.home() / "MailArchive" / "bodies.jsonl"
DEFAULT_BATCH = 1
MIN_LITERAL_CURL = (8, 17, 0)

HOMEBREW_CURL_CANDIDATES = (
    "/opt/homebrew/opt/curl/bin/curl",
    "/usr/local/opt/curl/bin/curl",
)

LIST_LINE = re.compile(
    r'^\* LIST \((?P<attrs>.*)\) (?P<delim>NIL|".") (?P<name>.+)\s*$'
)
SEARCH_LINE = re.compile(r"^\* SEARCH(?: (?P<uids>.*))?$", re.I)
FETCH_UID = re.compile(r"\bUID\s+(\d+)", re.I)
FETCH_SIZE = re.compile(r"\bRFC822\.SIZE\s+(\d+)", re.I)
FETCH_INTERNALDATE = re.compile(r'\bINTERNALDATE\s+"([^"]*)"', re.I)
FETCH_FLAGS = re.compile(r"\bFLAGS\s+\(([^)]*)\)", re.I)
FETCH_LITERAL = re.compile(
    r"(?is)\* \d+ FETCH \(.*?(?:BODY(?:\.PEEK)?\[\]|RFC822(?:\.PEEK)?) \{(\d+)\}\r?\n"
)
CURL_VERSION_RE = re.compile(r"\bcurl\s+(\d+)\.(\d+)(?:\.(\d+))?", re.I)
SIZE_DOWNLOAD_RE = re.compile(r"=======CURL_DL_BEGIN=======(\d+)=======CURL_DL_END=======")

APPLE_CURL_HINT = """\
Apple /usr/bin/curl 8.7.1 (and any curl < 8.17.0) truncates IMAP {{size}}
literals on custom UID FETCH BODY.PEEK[] (curl#18847; fix in 8.17.0).
Example: got 5 of 26973 bytes.

Install Homebrew curl (keg-only; do not use /usr/bin/curl for bodies):

  brew install curl
  export CURL_BIN="$(brew --prefix curl)/bin/curl"
  # Apple Silicon: /opt/homebrew/opt/curl/bin/curl
  # Intel:         /usr/local/opt/curl/bin/curl

  "$CURL_BIN" --version   # need {min}+ and Protocols: imap imaps
""".format(min="%d.%d.%d" % MIN_LITERAL_CURL)


class CurlImapError(RuntimeError):
    """curl imaps:// failed or returned a truncated literal."""


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    if not sys.stdin.isatty() and not sys.stderr.isatty():
        raise SystemExit(
            "Set IMAP_APP_PASSWORD or run in a terminal so a password can be prompted."
        )
    return getpass.getpass("iCloud IMAP app password: ")


def imap_user(explicit: str | None) -> str:
    user = explicit or os.environ.get("IMAP_USER") or ""
    user = user.strip()
    if not user:
        raise SystemExit("Set --user or IMAP_USER (iCloud IMAP username).")
    return user


def parse_curl_version(text: str) -> tuple[int, int, int] | None:
    match = CURL_VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def curl_has_imaps(blob: str) -> bool:
    return bool(re.search(r"\bimaps?\b", blob or "", re.I))


def supports_imap_literals(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version >= MIN_LITERAL_CURL


def inspect_curl(curl_bin: str) -> dict:
    result = subprocess.run(
        [curl_bin, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    version = parse_curl_version(blob)
    return {
        "path": curl_bin,
        "version": version,
        "version_text": (result.stdout or "").splitlines()[0] if result.stdout else "",
        "imaps": curl_has_imaps(blob),
        "literals": supports_imap_literals(version) and curl_has_imaps(blob),
        "blob": blob,
    }


def brew_prefix_curl() -> str | None:
    brew = shutil.which("brew")
    if not brew:
        return None
    result = subprocess.run(
        [brew, "--prefix", "curl"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    path = Path((result.stdout or "").strip()) / "bin" / "curl"
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def candidate_curl_bins(explicit: str | None = None) -> list[str]:
    if explicit:
        return [explicit]
    candidates: list[str] = []
    env = os.environ.get("CURL_BIN")
    if env:
        candidates.append(env)
    brew_curl = brew_prefix_curl()
    if brew_curl:
        candidates.append(brew_curl)
    candidates.extend(HOMEBREW_CURL_CANDIDATES)
    which = shutil.which("curl")
    if which:
        candidates.append(which)
    if sys.platform == "darwin":
        candidates.append("/usr/bin/curl")
    seen: set[str] = set()
    out: list[str] = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def find_curl(explicit: str | None = None) -> str:
    if explicit and not _is_executable(explicit):
        raise SystemExit("curl binary not executable: %s" % explicit)

    errors: list[str] = []
    fallback: str | None = None
    for path in candidate_curl_bins(explicit):
        if not _is_executable(path):
            continue
        try:
            info = inspect_curl(path)
        except OSError as exc:
            errors.append("%s: %s" % (path, exc))
            continue
        if not info["imaps"]:
            errors.append("%s: built without IMAP/IMAPS" % path)
            continue
        if info["literals"]:
            return path
        if fallback is None:
            fallback = path
            errors.append(
                "%s: version %s cannot stream BODY.PEEK[] literals"
                % (path, info["version"] or "unknown")
            )
    if explicit and fallback:
        return fallback
    if fallback:
        # Prefer reporting the Homebrew install path over silently using
        # Apple curl for a body fetch; callers decide via require_literals.
        return fallback
    hint = "\n".join(errors) if errors else "no curl binary found"
    raise SystemExit("Need a curl binary built with IMAPS.\n%s\n%s" % (APPLE_CURL_HINT, hint))


def require_literals(curl_bin: str) -> dict:
    info = inspect_curl(curl_bin)
    if not info["imaps"]:
        raise SystemExit("%s is not built with IMAP/IMAPS.\n%s" % (curl_bin, APPLE_CURL_HINT))
    if not info["literals"]:
        ver = "%d.%d.%d" % info["version"] if info["version"] else "unknown"
        raise SystemExit(
            "%s (%s) cannot stream IMAP BODY.PEEK[] literals.\n%s"
            % (curl_bin, ver, APPLE_CURL_HINT)
        )
    return info


def escape_curl_config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_out_token() -> str:
    return "=======CURL_DL_BEGIN=======%{size_download}=======CURL_DL_END======="


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
        'user = "%s:%s"'
        % (escape_curl_config_value(email), escape_curl_config_value(password)),
    ]
    for index, transfer in enumerate(transfers):
        if index:
            lines.append("next")
        lines.append('url = "%s"' % escape_curl_config_value(transfer["url"]))
        request = transfer.get("request")
        if request:
            lines.append('request = "%s"' % escape_curl_config_value(request))
        output = transfer.get("output")
        if output:
            lines.append('output = "%s"' % escape_curl_config_value(output))
        write_out = transfer.get("write_out")
        if write_out:
            lines.append('write-out = "%s"' % escape_curl_config_value(write_out))
    lines.append("")
    return "\n".join(lines)


def curl_argv(curl_bin: str) -> list[str]:
    return [curl_bin, "-K", "-", "--connect-timeout", "30"]


def redact(text: str, password: str) -> str:
    if password and password in text:
        return text.replace(password, "***")
    return text


def run_curl(curl_bin: str, config: str, *, password: str) -> str:
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
        err = redact((stderr or stdout).strip(), password)
        raise CurlImapError("curl exited %s: %s" % (result.returncode, err or "no stderr"))
    return stdout


def mailbox_url(mailbox: str, *, host: str = HOST, port: int = PORT) -> str:
    return "imaps://%s:%s/%s" % (host, port, quote(mailbox, safe="/"))


def message_url(
    mailbox: str,
    uid: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> str:
    if not str(uid).isdigit():
        raise ValueError("invalid IMAP UID: %r" % uid)
    return "%s;UID=%s" % (mailbox_url(mailbox, host=host, port=port), uid)


def fetch_peek_request(uid: str) -> str:
    if not str(uid).isdigit():
        raise ValueError("invalid IMAP UID: %r" % uid)
    return "UID FETCH %s (BODY.PEEK[])" % uid


def fetch_body_transfer(
    mailbox: str,
    uid: str,
    *,
    mode: str,
    host: str = HOST,
    port: int = PORT,
    output: str | None = None,
    write_out: str | None = None,
) -> dict:
    if mode == "uid-url":
        transfer = {"url": message_url(mailbox, uid, host=host, port=port)}
    else:
        transfer = {
            "url": mailbox_url(mailbox, host=host, port=port),
            "request": fetch_peek_request(uid),
        }
    if output:
        transfer["output"] = output
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
        match = LIST_LINE.match(raw_line.strip())
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


def parse_search_uids(text: str) -> list[str]:
    uids: list[str] = []
    for raw_line in text.splitlines():
        match = SEARCH_LINE.match(raw_line.strip())
        if not match:
            continue
        blob = (match.group("uids") or "").strip()
        uids.extend(part for part in blob.split() if part.isdigit())
    return uids


def parse_fetch_meta(text: str) -> dict[str, dict]:
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
            "flags": [part for part in (flags_match.group(1) if flags_match else "").split() if part],
            "internaldate": date_match.group(1) if date_match else None,
            "rfc822_size": int(size_match.group(1)) if size_match else None,
        }
    return records


def parse_size_downloads(write_out_text: str) -> list[int]:
    return [int(match) for match in SIZE_DOWNLOAD_RE.findall(write_out_text or "")]


def extract_rfc822_from_fetch(text: str) -> str:
    """Pull RFC822 bytes out of a curl FETCH response.

    Homebrew curl 8.17+ emits the IMAP wrapper plus the {size} literal.
    Native `;UID=` URL mode may emit the raw message only. A FETCH line
    with a {size} marker and fewer following bytes is Apple-curl
    truncation (curl#18847) — not a message.
    """
    if text == "":
        raise CurlImapError("empty FETCH")
    match = FETCH_LITERAL.search(text)
    if match:
        size = int(match.group(1))
        raw = text[match.end() : match.end() + size]
        if len(raw) < size:
            raise CurlImapError(
                "truncated FETCH literal: got %s of %s bytes. "
                "Apple /usr/bin/curl 8.7.1 does this (curl#18847). "
                "Use Homebrew curl 8.17+ via CURL_BIN=/opt/homebrew/opt/curl/bin/curl"
                % (len(raw), size)
            )
        return raw
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if first.startswith("*") and "FETCH" in first.upper():
        raise CurlImapError(
            "FETCH returned IMAP status only (no message literal). "
            "This curl may not stream BODY.PEEK[] literals."
        )
    return text


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
    out: list[str] = []
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


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


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
    text = "".join(text_parts)
    html = "".join(html_parts)
    if not text.strip() and html:
        text = html_to_text(html)
    return text, html


def record_from_rfc822(
    *,
    folder: str,
    uid: str,
    raw: str,
    meta: dict | None,
    include_raw: bool = True,
    size_download: int | None = None,
) -> dict:
    meta = meta or {}
    raw_bytes = raw.encode("latin-1", errors="replace")
    msg = message_from_bytes(raw_bytes)
    text, html = walk_body(msg)
    rec = {
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
        "rfc822_size": (
            meta.get("rfc822_size")
            if meta.get("rfc822_size") is not None
            else len(raw_bytes)
        ),
        "size_download": size_download,
    }
    if include_raw:
        rec["raw"] = raw
    return rec


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


def list_folders(
    curl_bin: str,
    user: str,
    password: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> list[dict]:
    config = build_curl_config(
        email=user,
        password=password,
        transfers=[{"url": "imaps://%s:%s/" % (host, port)}],
    )
    return parse_list_folders(run_curl(curl_bin, config, password=password))


def search_uids(
    curl_bin: str,
    user: str,
    password: str,
    mailbox: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> list[str]:
    config = build_curl_config(
        email=user,
        password=password,
        transfers=[
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID SEARCH ALL",
            }
        ],
    )
    return parse_search_uids(run_curl(curl_bin, config, password=password))


def fetch_meta(
    curl_bin: str,
    user: str,
    password: str,
    mailbox: str,
    *,
    host: str = HOST,
    port: int = PORT,
) -> dict[str, dict]:
    config = build_curl_config(
        email=user,
        password=password,
        transfers=[
            {
                "url": mailbox_url(mailbox, host=host, port=port),
                "request": "UID FETCH 1:* (UID FLAGS INTERNALDATE RFC822.SIZE)",
            }
        ],
    )
    return parse_fetch_meta(run_curl(curl_bin, config, password=password))


def fetch_bodies(
    curl_bin: str,
    user: str,
    password: str,
    mailbox: str,
    uids: list[str],
    *,
    mode: str,
    host: str = HOST,
    port: int = PORT,
) -> list[tuple[str, int | None]]:
    """Return (rfc822, size_download) per UID. Writes each body to a temp file."""
    if not uids:
        return []
    with tempfile.TemporaryDirectory(prefix="mailroom-imap-") as tmp:
        paths = [str(Path(tmp) / ("uid-%s.eml" % uid)) for uid in uids]
        transfers = [
            fetch_body_transfer(
                mailbox,
                uid,
                mode=mode,
                host=host,
                port=port,
                output=path,
                write_out=write_out_token(),
            )
            for uid, path in zip(uids, paths)
        ]
        config = build_curl_config(email=user, password=password, transfers=transfers)
        write_out = run_curl(curl_bin, config, password=password)
        sizes = parse_size_downloads(write_out)
        out: list[tuple[str, int | None]] = []
        for index, path in enumerate(paths):
            dest = Path(path)
            if not dest.is_file():
                raise CurlImapError("curl wrote no output file for UID %s" % uids[index])
            data = dest.read_bytes().decode("latin-1")
            raw = extract_rfc822_from_fetch(data)
            size_download = sizes[index] if index < len(sizes) else None
            out.append((raw, size_download))
        return out


def format_probe(info: dict) -> str:
    version = info.get("version")
    ver = "%d.%d.%d" % version if version else "unknown"
    lines = [
        "curl: %s" % info["path"],
        "version: %s" % (info.get("version_text") or ver),
        "imaps: %s" % ("yes" if info["imaps"] else "no"),
        "literal_fetch: %s" % ("yes (curl >= 8.17.0)" if info["literals"] else "no"),
    ]
    if not info["literals"]:
        lines.append("")
        lines.append(APPLE_CURL_HINT.rstrip())
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve iCloud IMAP bodies via curl BODY.PEEK[] "
            "(Homebrew curl 8.17+; no Python sockets)."
        )
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
        help="List folders and exit. Does not write JSONL or fetch bodies.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List folders + UIDs + RFC822.SIZE; do not FETCH BODY.PEEK[] or write JSONL.",
    )
    parser.add_argument(
        "--probe-curl",
        action="store_true",
        help="Print selected curl path/version/IMAPS/literal support and exit.",
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
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH,
        help="Messages per curl --next batch (default: %s)." % DEFAULT_BATCH,
    )
    parser.add_argument(
        "--curl-bin",
        default=None,
        help="curl binary. Default: CURL_BIN, then Homebrew curl, then PATH.",
    )
    parser.add_argument(
        "--user",
        dest="user",
        default=None,
        help="IMAP username. Default: IMAP_USER.",
    )
    parser.add_argument("--host", default=HOST, help="IMAP host (default: %s)" % HOST)
    parser.add_argument("--port", type=int, default=PORT, help="IMAPS port (default: %s)" % PORT)
    parser.add_argument(
        "--fetch-mode",
        choices=("peek", "uid-url"),
        default="peek",
        help="peek = UID FETCH BODY.PEEK[] (needs curl 8.17+). "
        "uid-url = imaps://mailbox;UID=n (BODY[], may set \\Seen).",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Omit the RFC822 blob from JSONL (keep text/headers for FTS).",
    )
    return parser.parse_args(argv)


def retrieve(args: argparse.Namespace) -> int:
    curl_bin = find_curl(args.curl_bin)
    info = inspect_curl(curl_bin)
    print(format_probe(info), file=sys.stderr)

    if args.probe_curl:
        return 0 if info["imaps"] else 2

    if args.fetch_mode == "peek" and not args.list_only and not args.dry_run:
        require_literals(curl_bin)

    user = imap_user(args.user)
    password = app_password()

    try:
        folders = list_folders(
            curl_bin, user, password, host=args.host, port=args.port
        )
    except CurlImapError as exc:
        raise SystemExit("LIST failed: %s" % exc) from exc

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

    output: Path | None = None
    seen: set[tuple[str, str]] = set()
    fh = None
    if not args.dry_run:
        output = args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        file_mode = "w"
        if output.exists() and not args.overwrite:
            seen = load_seen(output)
            file_mode = "a"
            print(
                "resume %s: %s message(s) already saved" % (output, len(seen)),
                file=sys.stderr,
            )
        fh = output.open(file_mode, encoding="utf-8")

    written = 0
    planned = 0
    batch_size = max(1, args.batch_size)
    try:
        for folder in selectable:
            name = folder["name"]
            try:
                uids = search_uids(
                    curl_bin, user, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print("skip folder %r: SEARCH failed: %s" % (name, exc), file=sys.stderr)
                continue
            try:
                meta = fetch_meta(
                    curl_bin, user, password, name, host=args.host, port=args.port
                )
            except CurlImapError as exc:
                print("folder %r: FLAGS fetch failed (%s)" % (name, exc), file=sys.stderr)
                meta = {}

            if args.dry_run:
                print("folder %s: %s uid(s)" % (name, len(uids)), file=sys.stderr)
                for uid in uids:
                    rec = meta.get(uid) or {}
                    size = rec.get("rfc822_size")
                    flags = " ".join(rec.get("flags") or [])
                    print(
                        "dry-run\t%s\tuid=%s\tsize=%s\tflags=%s"
                        % (name, uid, size, flags)
                    )
                    planned += 1
                    if args.max_messages and planned >= args.max_messages:
                        print(
                            "dry-run would fetch %s message(s)" % planned,
                            file=sys.stderr,
                        )
                        return 0
                continue

            pending = [uid for uid in uids if (name, uid) not in seen]
            print(
                "folder %s: %s uid(s), %s new" % (name, len(uids), len(pending)),
                file=sys.stderr,
            )
            if not pending:
                continue

            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                bodies: list[tuple[str, int | None] | None]
                try:
                    bodies = list(
                        fetch_bodies(
                            curl_bin,
                            user,
                            password,
                            name,
                            chunk,
                            mode=args.fetch_mode,
                            host=args.host,
                            port=args.port,
                        )
                    )
                except CurlImapError:
                    bodies = []
                    for uid in chunk:
                        try:
                            bodies.extend(
                                fetch_bodies(
                                    curl_bin,
                                    user,
                                    password,
                                    name,
                                    [uid],
                                    mode=args.fetch_mode,
                                    host=args.host,
                                    port=args.port,
                                )
                            )
                        except CurlImapError as exc:
                            print(
                                "skip %s uid=%s: %s" % (name, uid, exc),
                                file=sys.stderr,
                            )
                            bodies.append(None)

                assert fh is not None
                for uid, item in zip(chunk, bodies):
                    if item is None:
                        continue
                    raw, size_download = item
                    rec = record_from_rfc822(
                        folder=name,
                        uid=uid,
                        raw=raw,
                        meta=meta.get(uid),
                        include_raw=not args.no_raw,
                        size_download=size_download,
                    )
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
    finally:
        if fh is not None:
            fh.close()

    if args.dry_run:
        print("dry-run would fetch %s message(s)" % planned, file=sys.stderr)
        return 0
    print("wrote %s new message(s) to %s" % (written, output), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    return retrieve(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
