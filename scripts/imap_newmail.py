#!/usr/bin/env python3
"""Daily headers: new-mail IMAP (Apple curl). Copy-only SoR bind.

Honors --db then $MAILROOM_DB via mailroom_copy_db.bind_copy_db.
Allowlist: mailroom-copy.sqlite | mailroom-daily-copy.sqlite.
Unset / mailroom.sqlite refuse (fail closed). No silent SoR default.

GitHub contract is the bind. This process does not open IMAP, Keychain,
or a hardcoded SoR basename. Mini-local live IMAP should call
bind_copy_db() the same way before any sqlite write.

  /usr/bin/python3 imap_newmail.py --db /tmp/mailroom-copy.sqlite
  /usr/bin/python3 imap_newmail.py --db /tmp/mailroom.sqlite
"""

from __future__ import annotations

from mailroom_copy_db import bind_copy_db, child_main

__all__ = ["bind_copy_db", "main"]


def main(argv: list[str] | None = None) -> int:
    return child_main(argv, name="imap_newmail")


if __name__ == "__main__":
    raise SystemExit(main())
