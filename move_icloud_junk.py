#!/usr/bin/env python3
"""Dry-run iCloud junk mover. --apply is required to actually move.

Reads JSONL records with at least {folder, uid}. Default input is
~/Desktop/icloud_mail_junk.jsonl (override with --input). Destination
folder defaults to Junk.

Same login as the other Desktop scripts: imap.mail.me.com:993,
kirkbacon@me.com, password from IMAP_APP_PASSWORD or getpass.

Uses imap-tools MailBox().login(). Out of scope to run against the live
account. Does not write the app password to any file.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

HOST = "imap.mail.me.com"
PORT = 993
EMAIL = "kirkbacon@me.com"
DEFAULT_INPUT = Path.home() / "Desktop" / "icloud_mail_junk.jsonl"
DEFAULT_DEST = "Junk"


def mailbox_class():
    try:
        from imap_tools import MailBox
    except ImportError as exc:
        raise SystemExit(
            "move_icloud_junk.py needs imap-tools. pip install -r requirements.txt\n"
            f"({exc})"
        ) from exc
    return MailBox


def app_password() -> str:
    password = os.environ.get("IMAP_APP_PASSWORD")
    if password:
        return password
    return getpass.getpass("iCloud IMAP app password: ")


def load_targets(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"missing input JSONL: {path}")
    items: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON ({exc})") from exc
            folder = rec.get("folder")
            uid = rec.get("uid")
            if not folder or uid is None or uid == "":
                raise SystemExit(f"{path}:{line_no}: need {{folder, uid}}")
            items.append({"folder": str(folder), "uid": str(uid)})
    return items


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"JSONL with {{folder, uid}} (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST,
        help=f"Destination folder (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move messages. Without this flag, print a dry-run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    targets = load_targets(args.input)
    if not targets:
        print("no targets", file=sys.stderr)
        return 0

    password = app_password()
    with mailbox_class()(HOST, port=PORT).login(EMAIL, password) as mailbox:
        current = None
        for item in targets:
            folder = item["folder"]
            uid = item["uid"]
            print(
                f"{'MOVE' if args.apply else 'DRY-RUN'} {folder} uid={uid} -> {args.dest}"
            )
            if not args.apply:
                continue
            if current != folder:
                mailbox.folder.set(folder)
                current = folder
            mailbox.move(uid, args.dest)

    print(f"{len(targets)} message(s) {'moved' if args.apply else 'would move'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
