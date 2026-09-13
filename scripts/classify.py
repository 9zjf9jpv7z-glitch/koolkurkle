#!/usr/bin/env python3
"""Daily classify step. Copy-only SoR bind.

Honors --db then $MAILROOM_DB via mailroom_copy_db.bind_copy_db.
Allowlist: mailroom-copy.sqlite | mailroom-daily-copy.sqlite.
Unset / mailroom.sqlite refuse (fail closed). No silent SoR default.

GitHub contract is the bind. Classify work stays Mini-local; this
process does not invent labels or open IMAP/Keychain.

  /usr/bin/python3 classify.py --db /tmp/mailroom-copy.sqlite
  /usr/bin/python3 classify.py --db /tmp/mailroom.sqlite
"""

from __future__ import annotations

from mailroom_copy_db import bind_copy_db, child_main

__all__ = ["bind_copy_db", "main"]


def main(argv: list[str] | None = None) -> int:
    return child_main(argv, name="classify")


if __name__ == "__main__":
    raise SystemExit(main())
