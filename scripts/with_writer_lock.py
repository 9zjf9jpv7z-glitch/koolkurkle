#!/usr/bin/env python3
"""MAILROOM.md §9.5 exclusive writer lock for mailroom.sqlite.

Exclusive flock on ~/MailArchive/mailroom.write.lock. Writes PID, hostname,
purpose, and an ISO timestamp. If the lock is already held for more than
4 hours, refuse — do not steal.

Writers take this lock. ask_mail does not.
Action-required open (default: ~/MailArchive/ACTION_REQUIRED) ⇒ no lock,
no writes.

  with_writer_lock.py --purpose X -- cmd...

Not wired into embed_backfill in PR-0 (live embeds stay up).
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

DEFAULT_LOCK = Path.home() / "MailArchive" / "mailroom.write.lock"
DEFAULT_ACTION_REQUIRED = Path.home() / "MailArchive" / "ACTION_REQUIRED"
DEFAULT_MAX_AGE = timedelta(hours=4)


class WriterLockError(RuntimeError):
    """Lock refused (never includes secrets)."""


@dataclass(frozen=True)
class LockInfo:
    pid: int | None
    hostname: str
    purpose: str
    acquired_at: datetime | None
    raw: str

    def age(self, now: datetime) -> timedelta | None:
        if self.acquired_at is None:
            return None
        return now - self.acquired_at

    def summary(self) -> str:
        ts = self.acquired_at.isoformat() if self.acquired_at else "unknown"
        return "pid=%s host=%s purpose=%s acquired_at=%s" % (
            self.pid if self.pid is not None else "?",
            self.hostname or "?",
            self.purpose or "?",
            ts,
        )


@dataclass
class HeldLock:
    fd: IO[str]
    path: Path
    info: LockInfo


def default_lock_path() -> Path:
    raw = os.environ.get("MAILROOM_WRITE_LOCK")
    return Path(raw).expanduser() if raw else DEFAULT_LOCK


def default_action_required_path() -> Path:
    raw = os.environ.get("MAILROOM_ACTION_REQUIRED")
    return Path(raw).expanduser() if raw else DEFAULT_ACTION_REQUIRED


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_acquired_at(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_lock_payload(purpose: str, now: datetime, pid: int, hostname: str) -> str:
    return (
        "pid=%d\n"
        "hostname=%s\n"
        "purpose=%s\n"
        "acquired_at=%s\n"
        % (pid, hostname, purpose, now.isoformat())
    )


def parse_lock_payload(raw: str) -> LockInfo:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    pid_raw = fields.get("pid", "")
    try:
        pid = int(pid_raw) if pid_raw else None
    except ValueError:
        pid = None
    return LockInfo(
        pid=pid,
        hostname=fields.get("hostname", ""),
        purpose=fields.get("purpose", ""),
        acquired_at=parse_acquired_at(fields.get("acquired_at", "")),
        raw=raw,
    )


def read_lock_info(path: Path) -> LockInfo:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return LockInfo(pid=None, hostname="", purpose="", acquired_at=None, raw="")
    return parse_lock_payload(raw)


def action_required_open(path: Path) -> bool:
    return path.is_file()


def _write_lock_payload(fh: IO[str], purpose: str, now: datetime) -> LockInfo:
    payload = format_lock_payload(
        purpose=purpose,
        now=now,
        pid=os.getpid(),
        hostname=socket.gethostname(),
    )
    fh.seek(0)
    fh.truncate()
    fh.write(payload)
    fh.flush()
    os.fsync(fh.fileno())
    return parse_lock_payload(payload)


def acquire_writer_lock(
    path: Path,
    purpose: str,
    *,
    max_age: timedelta = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> HeldLock:
    """Exclusive flock. Refuse if held > max_age (no steal)."""
    if not purpose or not purpose.strip():
        raise WriterLockError("purpose is required")
    purpose = purpose.strip()
    when = now if now is not None else utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        info = read_lock_info(path)
        age = info.age(when)
        if age is not None and age > max_age:
            raise WriterLockError(
                "writer lock held >%sh, refuse (no steal): %s"
                % (int(max_age.total_seconds() // 3600), info.summary())
            ) from exc
        raise WriterLockError("writer lock held: %s" % info.summary()) from exc
    try:
        info = _write_lock_payload(fh, purpose, when)
    except Exception:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
        raise
    return HeldLock(fd=fh, path=path, info=info)


def release_writer_lock(held: HeldLock) -> None:
    try:
        fcntl.flock(held.fd.fileno(), fcntl.LOCK_UN)
    finally:
        held.fd.close()


def run_with_lock(
    purpose: str,
    cmd: list[str],
    *,
    lock_path: Path | None = None,
    action_required_path: Path | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> int:
    if action_required_open(action_required_path or default_action_required_path()):
        raise WriterLockError(
            "action-required open ⇒ no lock / no writes (%s)"
            % (action_required_path or default_action_required_path())
        )
    if not cmd:
        raise WriterLockError("command is required after --")
    held = acquire_writer_lock(
        lock_path or default_lock_path(),
        purpose,
        max_age=max_age,
        now=now,
    )
    try:
        completed = subprocess.run(cmd, check=False)
        return int(completed.returncode)
    finally:
        release_writer_lock(held)


def _strip_leading_ddash(cmd: list[str]) -> list[str]:
    if cmd and cmd[0] == "--":
        return cmd[1:]
    return cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hold an exclusive mailroom writer lock, then run a command. "
            "Refuse if held >4h (no steal). ask_mail does not use this."
        )
    )
    parser.add_argument(
        "--purpose",
        required=True,
        help="Why this writer holds the lock (stored in the lock file).",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help="Lock path (default: ~/MailArchive/mailroom.write.lock).",
    )
    parser.add_argument(
        "--action-required-file",
        default=None,
        help="If this file exists, refuse (default: ~/MailArchive/ACTION_REQUIRED).",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=4.0,
        help="Refuse (no steal) if the lock is already held longer than this.",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command to run while holding the lock (use -- before it).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = _strip_leading_ddash(list(args.cmd or []))
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else default_lock_path()
    action_path = (
        Path(args.action_required_file).expanduser()
        if args.action_required_file
        else default_action_required_path()
    )
    try:
        return run_with_lock(
            args.purpose,
            cmd,
            lock_path=lock_path,
            action_required_path=action_path,
            max_age=timedelta(hours=args.max_age_hours),
        )
    except WriterLockError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
