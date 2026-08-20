#!/usr/bin/env python3
"""File iCloud INBOX messages into Receipts / Has Attachments / Old Unsubscribe.

Reconstructed from the Aug 13 Desktop script. Auth is IMAP_APP_PASSWORD or a
getpass prompt. Uses imap-tools MailBox().login() — the same path that now
fails on the owner's Mac with OSError [Errno 9] EBADF during socket connect,
before LOGIN.

This script is kept as the Desktop filer. It is not the Mac retrieve-all
tool. On that Mac, use /usr/bin/python3 retrieve_mail_imaplib.py.

Does not write the app password to any file. Requires --apply to move mail.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HOST = "imap.mail.me.com"
PORT = 993
EMAIL = "kirkbacon@me.com"

FOLDER_RECEIPTS = "Receipts"
FOLDER_ATTACHMENTS = "Has Attachments"
FOLDER_UNSUBSCRIBE = "Old Unsubscribe"
DEST_FOLDERS = (FOLDER_RECEIPTS, FOLDER_ATTACHMENTS, FOLDER_UNSUBSCRIBE)

UNSUBSCRIBE_AGE = timedelta(days=365)

RECEIPT_SUBJECT = re.compile(
    r"(receipt|invoice|order confirmation|your order|payment received|"
    r"order #|billing statement|thanks for your (order|purchase))",
    re.I,
)
RECEIPT_FROM = re.compile(
    r"(apple\.com|itunes\.com|amazon\.|paypal\.|stripe\.|squareup\.|"
    r"receipts@|invoice@|billing@|noreply@.*shop)",
    re.I,
)


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    return getpass.getpass("iCloud IMAP app password: ")


def header_map(msg) -> dict[str, str]:
    out: dict[str, str] = {}
    headers = getattr(msg, "headers", {}) or {}
    for key, value in headers.items():
        if isinstance(value, (list, tuple)):
            text = " ".join(str(part) for part in value)
        else:
            text = str(value)
        out[key.lower()] = text
    return out


def is_old(msg, now: datetime) -> bool:
    date = getattr(msg, "date", None)
    if date is None:
        return False
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return (now - date) >= UNSUBSCRIBE_AGE


def destination_for(msg, now: datetime) -> str | None:
    """First match wins: attachments, receipts, then old unsubscribe."""
    if getattr(msg, "attachments", None):
        return FOLDER_ATTACHMENTS
    subject = getattr(msg, "subject", "") or ""
    from_ = getattr(msg, "from_", "") or ""
    if RECEIPT_SUBJECT.search(subject) or RECEIPT_FROM.search(from_):
        return FOLDER_RECEIPTS
    headers = header_map(msg)
    if "list-unsubscribe" in headers and is_old(msg, now):
        return FOLDER_UNSUBSCRIBE
    return None


def mailbox_class():
    try:
        from imap_tools import MailBox
    except ImportError as exc:
        raise SystemExit(
            "icloud_mail.py needs imap-tools. pip install -r requirements.txt\n"
            f"({exc})"
        ) from exc
    return MailBox


def ensure_folders(mailbox) -> None:
    existing = {info.name for info in mailbox.folder.list()}
    for name in DEST_FOLDERS:
        if name not in existing:
            mailbox.folder.create(name)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move messages. Without this flag, print a dry-run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    password = app_password()

    with mailbox_class()(HOST, port=PORT).login(EMAIL, password, initial_folder="INBOX") as mailbox:
        if args.apply:
            ensure_folders(mailbox)
            mailbox.folder.set("INBOX")

        planned: list[tuple[str, str]] = []
        for msg in mailbox.fetch(mark_seen=False):
            dest = destination_for(msg, now)
            if dest:
                planned.append((msg.uid, dest))

        for uid, dest in planned:
            print(f"{'MOVE' if args.apply else 'DRY-RUN'} INBOX uid={uid} -> {dest}")
            if args.apply:
                mailbox.move(uid, dest)

        print(f"{len(planned)} message(s) {'moved' if args.apply else 'would move'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
