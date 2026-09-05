#!/usr/bin/env python3
"""Headers-only iCloud IMAP (ENVELOPE / LIST / SEARCH / tombstone).

Defaults to Apple /usr/bin/curl. Homebrew curl often fails
imap.mail.me.com:993 with EBADF / Bad file descriptor on this path.
Honor CURL_BIN if set.

BODY.PEEK body-fetch scripts (imap_fetch_bodies / imap_fetch_bodies_fts)
pick Homebrew curl themselves — do not use this module's CURL_BIN for
literal body downloads.
"""

from __future__ import annotations

import argparse
import os
import sys

# Headers-only / ENVELOPE / LIST / SEARCH: prefer Apple /usr/bin/curl.
# Homebrew curl often fails imap.mail.me.com:993 with EBADF / Bad file descriptor.
# Override with CURL_BIN=... if needed. BODY.PEEK body-fetch scripts pick brew themselves.
APPLE_CURL = "/usr/bin/curl"
BREW_CURL = "/opt/homebrew/opt/curl/bin/curl"


def select_curl_bin() -> str:
    _env_curl = os.environ.get("CURL_BIN")
    if _env_curl:
        return _env_curl
    if os.path.isfile(APPLE_CURL) and os.access(APPLE_CURL, os.X_OK):
        return APPLE_CURL
    if os.path.isfile(BREW_CURL) and os.access(BREW_CURL, os.X_OK):
        return BREW_CURL
    return APPLE_CURL


CURL_BIN = select_curl_bin()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Headers-only IMAP curl picker. Default is Apple /usr/bin/curl; "
            "CURL_BIN overrides. Does not fetch BODY.PEEK[] literals."
        )
    )
    parser.add_argument(
        "--print-curl",
        action="store_true",
        help="Print the selected curl binary and exit (8pm / imap_newmail / tombstone).",
    )
    parser.parse_args(argv)
    sys.stdout.write(select_curl_bin() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
