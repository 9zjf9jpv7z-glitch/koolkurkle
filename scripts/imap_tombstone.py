#!/usr/bin/env python3
"""Daily headers: IMAP tombstone (Apple curl). Copy-only SoR bind.

bind_copy_db is the child entry used on Mini: resolve --db /
$MAILROOM_DB, export MAILROOM_DB, refuse mailroom.sqlite and unset.
argv=None reads sys.argv[1:] so a bare bind_copy_db() still sees --db.

Allowlist: mailroom-copy.sqlite | mailroom-daily-copy.sqlite.
Unset / mailroom.sqlite refuse (fail closed). No IMAP sockets, no
Keychain, no silent default to the SoR name.

  /usr/bin/python3 imap_tombstone.py --db /tmp/mailroom-copy.sqlite
  /usr/bin/python3 imap_tombstone.py --db /tmp/mailroom.sqlite
"""

from __future__ import annotations

from mailroom_copy_db import bind_copy_db, child_main

__all__ = ["bind_copy_db", "main"]


def main(argv: list[str] | None = None) -> int:
    return child_main(argv, name="imap_tombstone")


if __name__ == "__main__":
    raise SystemExit(main())
