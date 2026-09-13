#!/usr/bin/env python3
"""Daily body/FTS: IMAP BODY.PEEK + FTS. Copy-only SoR bind.

Honors --db then $MAILROOM_DB via mailroom_copy_db.bind_copy_db.
Allowlist: mailroom-copy.sqlite | mailroom-daily-copy.sqlite.
Unset / mailroom.sqlite refuse (fail closed). No silent SoR default.

The daily driver unsets CURL_BIN so a Mini-local body script can pick
Homebrew curl >= 8.17. This GitHub contract is the bind only — no IMAP,
no Keychain, no hardcoded SoR path.

  /usr/bin/python3 imap_fetch_bodies_fts.py --db /tmp/mailroom-copy.sqlite
  /usr/bin/python3 imap_fetch_bodies_fts.py --db /tmp/mailroom.sqlite
"""

from __future__ import annotations

from mailroom_copy_db import bind_copy_db, child_main

__all__ = ["bind_copy_db", "main"]


def main(argv: list[str] | None = None) -> int:
    return child_main(argv, name="imap_fetch_bodies_fts")


if __name__ == "__main__":
    raise SystemExit(main())
