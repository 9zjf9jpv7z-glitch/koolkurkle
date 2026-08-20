#!/usr/bin/env python3
"""Retrieve every iCloud IMAP folder into ~/Desktop/icloud_mail_all.jsonl.

This is the Desktop retrieve-all that uses imap-tools MailBox().login().
A MailBoxIPv4 pin to 17.42.251.69 and a Python 3.14 hard-exit were already
tried on the owner's Mac. Both failed: Homebrew Python raises
OSError [Errno 9] EBADF during socket connect, before LOGIN.

Do not use this file as the Mac retrieve command. Use retrieve_mail_curl.py,
which shells out to curl imaps:// so Python never opens the IMAP socket.

Does not mark mail read. Does not move mail. Does not write the app password
to any file. Password: IMAP_APP_PASSWORD or getpass.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import timezone
from email.header import decode_header, make_header
from pathlib import Path

HOST = "imap.mail.me.com"
PORT = 993
EMAIL = "kirkbacon@me.com"
ICLOUD_IMAP_IPV4 = "17.42.251.69"
DEFAULT_OUTPUT = Path.home() / "Desktop" / "icloud_mail_all.jsonl"


def mailbox_classes():
    try:
        from imap_tools import MailBox
    except ImportError as exc:
        raise SystemExit(
            "retrieve_mail.py needs imap-tools. pip install -r requirements.txt\n"
            f"({exc})"
        ) from exc

    class MailBoxIPv4(MailBox):
        """Failed workaround: pin TLS to a hardcoded IPv4 address.

        The owner's Mac still raised EBADF during connect after this pin.
        Kept so the repo matches the Desktop script that already failed.
        """

        def __init__(self, host: str = HOST, port: int = PORT, ipv4: str = ICLOUD_IMAP_IPV4, **kwargs):
            self._ipv4 = ipv4
            self._tls_hostname = host
            super().__init__(host=ipv4, port=port, **kwargs)

        def _get_mailbox_client(self):
            client = super()._get_mailbox_client()
            try:
                client.host = self._tls_hostname
            except Exception:
                pass
            return client

    return MailBox, MailBoxIPv4


def _hard_exit_if_python_314() -> None:
    """Failed workaround: refuse 3.14 instead of connecting.

    This avoided the EBADF traceback and also retrieved no mail.
    """
    if sys.version_info >= (3, 14):
        sys.stderr.write(
            "Python 3.14 on this Mac raises EBADF on IMAP sockets "
            f"(you have {sys.version.split()[0]}).\n"
            "Use retrieve_mail_curl.py (curl imaps://) instead.\n"
        )
        raise SystemExit(2)


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    return getpass.getpass("iCloud IMAP app password: ")


def _jsonable_headers(headers) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in (headers or {}).items():
        if isinstance(value, (list, tuple)):
            out[str(key)] = [str(part) for part in value]
        else:
            out[str(key)] = [str(value)]
    return out


def _decode(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            return str(make_header(decode_header(str(value))))
        except Exception:
            return str(value)
    return value


def _date_iso(msg) -> str | None:
    date = getattr(msg, "date", None)
    if date is None:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.isoformat()


def message_record(folder: str, msg) -> dict:
    raw = ""
    obj = getattr(msg, "obj", None)
    if obj is not None:
        try:
            raw = obj.as_bytes().decode("latin-1")
        except Exception:
            raw = ""
    to_vals = getattr(msg, "to", ()) or ()
    cc_vals = getattr(msg, "cc", ()) or ()
    flags = getattr(msg, "flags", ()) or ()
    return {
        "folder": folder,
        "uid": str(msg.uid),
        "flags": [str(flag) for flag in flags],
        "internaldate": None,
        "date": _date_iso(msg),
        "from": _decode(getattr(msg, "from_", "")),
        "to": [_decode(addr) for addr in to_vals],
        "cc": [_decode(addr) for addr in cc_vals],
        "subject": _decode(getattr(msg, "subject", "")),
        "message_id": "",
        "text": getattr(msg, "text", "") or "",
        "html": getattr(msg, "html", "") or "",
        "rfc822_size": len(raw.encode("latin-1")) if raw else None,
        "raw": raw,
        "headers": _jsonable_headers(getattr(msg, "headers", {})),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--ipv4",
        action="store_true",
        help="Use the failed MailBoxIPv4 pin to 17.42.251.69",
    )
    parser.add_argument(
        "--allow-python-314",
        action="store_true",
        help="Skip the 3.14 hard-exit (still uses Python sockets)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_python_314:
        _hard_exit_if_python_314()

    password = app_password()
    mailbox_cls, mailbox_ipv4_cls = mailbox_classes()
    box_cls = mailbox_ipv4_cls if args.ipv4 else mailbox_cls
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with box_cls(HOST, port=PORT).login(EMAIL, password) as mailbox:
        folders = list(mailbox.folder.list())
        with output.open("w", encoding="utf-8") as fh:
            for info in folders:
                name = info.name
                flags = {str(flag).lstrip("\\").lower() for flag in (info.flags or ())}
                if "noselect" in flags:
                    continue
                try:
                    mailbox.folder.set(name)
                except Exception as exc:
                    print(f"skip folder {name!r}: {exc}", file=sys.stderr)
                    continue
                print(f"folder {name}", file=sys.stderr)
                for msg in mailbox.fetch(mark_seen=False):
                    fh.write(json.dumps(message_record(name, msg), ensure_ascii=False))
                    fh.write("\n")
                    written += 1

    print(f"wrote {written} message(s) to {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
