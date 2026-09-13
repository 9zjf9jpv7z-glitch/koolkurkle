#!/usr/bin/env python3
"""Copy-only MAILROOM_DB helper for Mini daily children.

Same allowlist as mailroom_daily.py. Honor --db, then $MAILROOM_DB.
Refuse mailroom.sqlite / unset until SoR cutover (PR-5). Hard-fail,
not fail-open. No silent default to the SoR name.

Daily children (imap_newmail, imap_tombstone, imap_fetch_bodies_fts /
imap_fetch_bodies, classify, notify_bills) resolve the DB through
bind_copy_db() / resolve_from_argv() instead of a hardcoded SoR path
(t.DB or ~/MailArchive/mailroom.sqlite). argv=None means sys.argv[1:]
— otherwise --db on the process command line is ignored and a child
can still open Mini's empty SoR stub. embed_backfill.py already
accepts --db; the orchestrator still passes --db and MAILROOM_DB so
every child opens the same copy as the driver.

  /usr/bin/python3 mailroom_copy_db.py --db /tmp/mailroom-copy.sqlite
  /usr/bin/python3 mailroom_copy_db.py --db /tmp/mailroom.sqlite
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

COPY_DB_BASENAMES = frozenset(
    {
        "mailroom-copy.sqlite",
        "mailroom-daily-copy.sqlite",
    }
)
ALLOWLIST_HELP = "mailroom-copy.sqlite or mailroom-daily-copy.sqlite"
SOR_BASENAME = "mailroom.sqlite"


class CopyDbRefuse(RuntimeError):
    """Hard refuse (db_mode=refused). No silent SoR default."""


def allowed_copy_db(path: Path) -> bool:
    return path.name in COPY_DB_BASENAMES


def env_db_path() -> Path | None:
    """Return MAILROOM_DB if set. Never silently default to mailroom.sqlite."""
    raw = (os.environ.get("MAILROOM_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return None


def unset_db_message() -> str:
    return (
        "MAILROOM_DB is unset. Set an explicit copy path (basename %s). "
        "Preferred practice: the Mini daily job writes only a copy until "
        "SoR cutover (PR-5). A silent default to mailroom.sqlite would "
        "write the SoR name (empty on Mini, or race rem embed)."
        % ALLOWLIST_HELP
    )


def refuse_copy_db_message(path: Path) -> str:
    return (
        "MAILROOM_DB basename %r is not on the copy allowlist (%s). "
        "Refusing start until SoR cutover (PR-5). No IMAP/embed. "
        "Use mailroom-copy.sqlite, or mailroom-daily-copy.sqlite when "
        "rem embed still holds the copy."
        % (path.name, ALLOWLIST_HELP)
    )


def resolve_copy_db(cli_db: str | None = None) -> Path:
    """Hard-fail unless basename is on the copy allowlist.

    Precedence: --db / cli_db, then $MAILROOM_DB. Unset and SoR refuse.
    """
    if cli_db:
        path = Path(str(cli_db)).expanduser()
    else:
        path = env_db_path()
        if path is None:
            raise CopyDbRefuse(unset_db_message())
    if not allowed_copy_db(path):
        raise CopyDbRefuse(refuse_copy_db_message(path))
    return path


def parse_db_cli(argv: list[str] | None) -> str | None:
    """Return --db value from argv, or None. Ignores other flags.

    argv=None reads sys.argv[1:] (the process command line without the
    program name). An explicit empty list means "no CLI flags". Treating
    None like [] would ignore --db on the real command line.
    """
    if argv is None:
        argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--db":
            if i + 1 >= len(argv):
                raise CopyDbRefuse("MAILROOM_DB --db requires a path")
            return argv[i + 1]
        if arg.startswith("--db="):
            return arg.split("=", 1)[1]
        i += 1
    return None


def resolve_from_argv(argv: list[str] | None = None) -> Path:
    """Resolve copy DB from argv --db, else $MAILROOM_DB. Same allowlist.

    argv=None reads sys.argv[1:].
    """
    return resolve_copy_db(parse_db_cli(argv))


def bind_copy_db(argv: list[str] | None = None) -> Path:
    """Resolve the copy DB, export MAILROOM_DB, emit db_mode=copy.

    Daily children call this before any sqlite open. argv=None means
    sys.argv[1:] so ``bind_copy_db()`` honors a process-level --db.
    Raises CopyDbRefuse for unset / mailroom.sqlite / other names.
    """
    path = resolve_from_argv(argv)
    os.environ["MAILROOM_DB"] = str(path)
    emit_db_mode("copy")
    return path


def child_main(argv: list[str] | None = None, *, name: str = "child") -> int:
    """Shared daily-child entry: bind copy DB, print opened_db, fail closed.

    No IMAP, Keychain, or classify work. GitHub contract is SoR bind.
    Mini-local live bodies should call bind_copy_db() the same way.
    """
    del name
    try:
        path = bind_copy_db(argv)
    except CopyDbRefuse as exc:
        emit_db_mode("refused")
        sys.stderr.write("error: %s\n" % exc)
        return 2
    sys.stdout.write("opened_db=%s\n" % path)
    return 0


def emit_db_mode(mode: str) -> None:
    sys.stderr.write("db_mode=%s\n" % mode)
    sys.stderr.flush()


def child_would_open_sor(
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    """True if a child would open the SoR basename (or has no copy path).

    Used by negative smoke. A daily child honors --db / MAILROOM_DB via
    the same allowlist; hardcoded mailroom.sqlite is a fail.
    """
    saved = None
    if env is not None:
        saved = os.environ.get("MAILROOM_DB")
        raw = (env.get("MAILROOM_DB") or "").strip()
        if raw:
            os.environ["MAILROOM_DB"] = raw
        else:
            os.environ.pop("MAILROOM_DB", None)
    try:
        try:
            path = resolve_from_argv(argv)
        except CopyDbRefuse:
            cli = parse_db_cli(argv)
            if cli and Path(cli).expanduser().name == SOR_BASENAME:
                return True
            env_path = env_db_path()
            if env_path is not None and env_path.name == SOR_BASENAME:
                return True
            if cli is None and env_path is None:
                return True
            return False
        return path.name == SOR_BASENAME
    finally:
        if env is not None:
            if saved is None:
                os.environ.pop("MAILROOM_DB", None)
            else:
                os.environ["MAILROOM_DB"] = saved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve a Mini daily copy DB (--db or $MAILROOM_DB). "
            "Allowlist: mailroom-copy.sqlite, mailroom-daily-copy.sqlite. "
            "Unset / mailroom.sqlite refused until SoR cutover."
        )
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Copy DB path (overrides $MAILROOM_DB). Same allowlist as the daily driver.",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_path",
        help="Print the resolved path on stdout (default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = resolve_copy_db(args.db)
    except CopyDbRefuse as exc:
        emit_db_mode("refused")
        sys.stderr.write("error: %s\n" % exc)
        return 2
    emit_db_mode("copy")
    sys.stdout.write(str(path) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
