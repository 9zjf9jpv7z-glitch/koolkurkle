#!/usr/bin/env python3
"""Mini daily RAG orchestrator: IMAP headers → FTS → classify/bills → embed.

Wires existing MailArchive scripts. Does not reimplement IMAP, FTS, classify,
or embed. No Python IMAP sockets. No secrets.

Catch-up: if ~/MailArchive/logs/last_daily_rag_ok is missing or at least 24h
old, run the chain; else exit 0 quietly. Stamp is written only after every
step returns 0.

  /usr/bin/python3 mailroom_daily.py --print-plan
  /usr/bin/python3 mailroom_daily.py --skip-if-fresh
  /usr/bin/python3 mailroom_daily.py --force
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

APPLE_CURL = "/usr/bin/curl"
APPLE_PY = "/usr/bin/python3"
DEFAULT_ARCHIVE = Path.home() / "MailArchive"
STAMP_NAME = "last_daily_rag_ok"
CATCH_UP_MAX_AGE_SEC = 24 * 60 * 60
# Calendar fire can be a few minutes early vs last night's stamp.
CATCH_UP_SLOP_SEC = 15 * 60

HEADER_SCRIPTS = ("imap_newmail.py", "imap_tombstone.py")
BODY_SCRIPTS = ("imap_fetch_bodies_fts.py", "imap_fetch_bodies.py")
CLASSIFY_SCRIPT = "classify.py"
BILLS_SCRIPT = "notify_bills.py"
EMBED_SCRIPT = "embed_backfill.py"


class DailyError(RuntimeError):
    """Orchestrator failure (never includes secrets)."""


@dataclass(frozen=True)
class Step:
    name: str
    scripts: tuple[str, ...]
    python: str  # "apple" | "venv"
    curl: str  # "apple" | "unset" | "inherit"
    extra_args: tuple[str, ...] = ()
    first_only: bool = False  # run the first existing script, not all


@dataclass
class PlanItem:
    step: str
    script: Path
    python: Path
    argv: list[str]
    env_notes: str
    extra_env: dict[str, str] = field(default_factory=dict)
    unset_env: tuple[str, ...] = ()


def default_archive() -> Path:
    raw = os.environ.get("MAILARCHIVE")
    return Path(raw).expanduser() if raw else DEFAULT_ARCHIVE


def default_scripts_dir(archive: Path) -> Path:
    raw = os.environ.get("MAILARCHIVE_SCRIPTS")
    if raw:
        return Path(raw).expanduser()
    return archive / "scripts"


def default_logs_dir(archive: Path) -> Path:
    raw = os.environ.get("MAILARCHIVE_LOGS")
    if raw:
        return Path(raw).expanduser()
    return archive / "logs"


def default_db_path(archive: Path) -> Path:
    raw = os.environ.get("MAILROOM_DB")
    if raw:
        return Path(raw).expanduser()
    return archive / "mailroom.sqlite"


def stamp_path(logs_dir: Path) -> Path:
    return logs_dir / STAMP_NAME


def stamp_age_seconds(path: Path, now: float | None = None) -> float | None:
    """Return age in seconds, or None if the stamp file is missing."""
    if not path.is_file():
        return None
    now_ts = time.time() if now is None else now
    return now_ts - path.stat().st_mtime


def should_run_pipeline(
    path: Path,
    now: float | None = None,
    stale_sec: int = CATCH_UP_MAX_AGE_SEC,
    slop_sec: int = CATCH_UP_SLOP_SEC,
) -> bool:
    """True when stamp is missing or old enough that catch-up / daily should run.

    Threshold is 24h minus a small slop so StartCalendarInterval at ~20:00 is
    not skipped when last night's stamp is 23h 50m old. RunAtLoad the same
    evening or next morning still exits 0.
    """
    age = stamp_age_seconds(path, now=now)
    if age is None:
        return True
    threshold = max(0, stale_sec - slop_sec)
    return age >= threshold


def write_ok_stamp(path: Path, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time() if now is None else now
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    payload = when.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    path.write_text(payload, encoding="utf-8")
    os.utime(path, (ts, ts))


def apple_python() -> Path:
    env = os.environ.get("MAILROOM_APPLE_PY")
    if env:
        return Path(env).expanduser()
    if Path(APPLE_PY).is_file() and os.access(APPLE_PY, os.X_OK):
        return Path(APPLE_PY)
    found = shutil.which("python3")
    if found:
        return Path(found)
    return Path(sys.executable)


def venv_python(archive: Path) -> Path:
    env = os.environ.get("MAILROOM_VENV_PY")
    if env:
        return Path(env).expanduser()
    return archive / ".venv" / "bin" / "python"


def refuse_apple_python_for_embed(py: Path) -> None:
    resolved = str(py.resolve()) if py.exists() else str(py)
    if resolved == APPLE_PY or str(py) == APPLE_PY:
        raise DailyError(
            "embed step refuses Apple /usr/bin/python3 (cannot load sqlite-vec). "
            "Use ~/MailArchive/.venv/bin/python (PEP 668)."
        )


def search_roots(archive: Path, scripts_dir: Path) -> list[Path]:
    here = Path(__file__).resolve().parent
    repo = here.parent
    roots = [
        scripts_dir,
        here,
        repo / "mailroom",
        repo,
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def find_script(name: str, roots: list[Path]) -> Path | None:
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def pipeline_steps() -> tuple[Step, ...]:
    return (
        Step(
            name="headers",
            scripts=HEADER_SCRIPTS,
            python="apple",
            curl="apple",
            first_only=False,
        ),
        Step(
            name="bodies-fts",
            scripts=BODY_SCRIPTS,
            python="apple",
            curl="unset",
            first_only=True,
        ),
        Step(
            name="classify",
            scripts=(CLASSIFY_SCRIPT,),
            python="apple",
            curl="inherit",
        ),
        Step(
            name="bills",
            scripts=(BILLS_SCRIPT,),
            python="apple",
            curl="inherit",
        ),
        Step(
            name="embed",
            scripts=(EMBED_SCRIPT,),
            python="venv",
            curl="inherit",
            extra_args=("--skip-auth", "--quote-strip", "--lock"),
        ),
    )


def resolve_python(kind: str, archive: Path) -> Path:
    if kind == "venv":
        py = venv_python(archive)
        refuse_apple_python_for_embed(py)
        return py
    return apple_python()


def build_plan(
    archive: Path,
    scripts_dir: Path,
    db: Path,
    extra_embed_args: tuple[str, ...] = (),
) -> list[PlanItem]:
    roots = search_roots(archive, scripts_dir)
    items: list[PlanItem] = []
    missing: list[str] = []
    for step in pipeline_steps():
        found: list[Path] = []
        for name in step.scripts:
            path = find_script(name, roots)
            if path is not None:
                found.append(path)
                if step.first_only:
                    break
        if not found:
            missing.append("%s (%s)" % (step.name, " | ".join(step.scripts)))
            continue
        py = resolve_python(step.python, archive)
        extra_env: dict[str, str] = {}
        unset_env: tuple[str, ...] = ()
        if step.curl == "apple":
            extra_env["CURL_BIN"] = APPLE_CURL
            note = "CURL_BIN=/usr/bin/curl (headers-only Apple curl)"
        elif step.curl == "unset":
            unset_env = ("CURL_BIN",)
            note = "CURL_BIN unset (body/FTS script picks Homebrew curl >=8.17)"
        else:
            note = "CURL_BIN inherited"
        extra = list(step.extra_args)
        if step.name == "embed":
            extra.extend(("--db", str(db)))
            extra.extend(extra_embed_args)
            note = "%s; python=%s (sqlite-vec)" % (note, py)
        for script in found:
            argv = [str(py), str(script), *extra]
            items.append(
                PlanItem(
                    step=step.name,
                    script=script,
                    python=py,
                    argv=argv,
                    env_notes=note,
                    extra_env=extra_env,
                    unset_env=unset_env,
                )
            )
    if missing:
        raise DailyError(
            "missing required MailArchive scripts: %s. "
            "Copy the Mini-local 8pm chain and embed_backfill.py into %s."
            % (", ".join(missing), scripts_dir)
        )
    return items


def format_plan(items: list[PlanItem]) -> str:
    lines = ["mailroom daily plan (Mini-local, no cloud):"]
    for i, item in enumerate(items, start=1):
        lines.append(
            "%d. %s  %s" % (i, item.step, item.script)
        )
        lines.append("   exec: %s" % " ".join(item.argv))
        lines.append("   %s" % item.env_notes)
    return "\n".join(lines) + "\n"


def child_env(item: PlanItem) -> dict[str, str]:
    env = os.environ.copy()
    for key in item.unset_env:
        env.pop(key, None)
    env.update(item.extra_env)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Never log secrets; IMAP_APP_PASSWORD may be present from the zsh wrapper.
    return env


def run_step(item: PlanItem, dry_run: bool, log) -> int:
    log("step start: %s (%s)" % (item.step, item.script.name))
    log("  %s" % item.env_notes)
    if dry_run:
        log("  dry-run skip exec")
        return 0
    if not item.python.exists() and not shutil.which(str(item.python)):
        raise DailyError("python not found for step %s: %s" % (item.step, item.python))
    if item.step == "embed":
        refuse_apple_python_for_embed(item.python)
        if not item.python.is_file():
            raise DailyError(
                "Mini embed python missing: %s. Create ~/MailArchive/.venv "
                "(PEP 668) — Apple /usr/bin/python3 cannot load sqlite-vec."
                % item.python
            )
    result = subprocess.run(item.argv, env=child_env(item), check=False)
    log("step exit: %s rc=%s" % (item.step, result.returncode))
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mini daily RAG: headers (Apple curl) → body/FTS → classify → "
            "bills → incremental embed. Stamp last_daily_rag_ok only on EXIT 0."
        )
    )
    parser.add_argument(
        "--archive",
        default=str(default_archive()),
        help="MailArchive root (default: ~/MailArchive or $MAILARCHIVE).",
    )
    parser.add_argument(
        "--scripts",
        default=None,
        help="Scripts dir (default: $MAILARCHIVE_SCRIPTS or <archive>/scripts).",
    )
    parser.add_argument(
        "--logs",
        default=None,
        help="Logs dir (default: $MAILARCHIVE_LOGS or <archive>/logs).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Driver DB (default: $MAILROOM_DB or <archive>/mailroom.sqlite).",
    )
    parser.add_argument(
        "--skip-if-fresh",
        action="store_true",
        default=True,
        help="Exit 0 if last_daily_rag_ok is younger than 24h (default).",
    )
    parser.add_argument(
        "--no-skip-if-fresh",
        action="store_false",
        dest="skip_if_fresh",
        help="Always run the chain (ignore stamp age). Does not skip stamp write.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Same as --no-skip-if-fresh.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the plan and log steps; do not exec children or write stamp.",
    )
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="Print the resolved command plan and exit 0 (no stamp).",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="With --print-plan / --dry-run, do not fail if Mini scripts are absent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    archive = Path(args.archive).expanduser()
    scripts_dir = (
        Path(args.scripts).expanduser()
        if args.scripts
        else default_scripts_dir(archive)
    )
    logs_dir = Path(args.logs).expanduser() if args.logs else default_logs_dir(archive)
    db = Path(args.db).expanduser() if args.db else default_db_path(archive)
    stamp = stamp_path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = msg.rstrip("\n")
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        dated = logs_dir / ("daily_rag_%s.log" % datetime.now().strftime("%Y-%m-%d"))
        try:
            with dated.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    force = bool(args.force or not args.skip_if_fresh)
    if not force and not args.print_plan and not args.dry_run:
        if not should_run_pipeline(stamp):
            # Quiet skip — LaunchAgent RunAtLoad catch-up.
            return 0

    try:
        items = build_plan(archive, scripts_dir, db)
    except DailyError as exc:
        if args.allow_missing and (args.print_plan or args.dry_run):
            sys.stdout.write("plan incomplete: %s\n" % exc)
            return 0
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if args.print_plan:
        sys.stdout.write(format_plan(items))
        return 0

    log("mailroom daily start archive=%s db=%s" % (archive, db))
    log("stamp=%s" % stamp)
    for item in items:
        rc = run_step(item, dry_run=args.dry_run, log=log)
        if rc != 0:
            log("chain aborted; not writing %s" % STAMP_NAME)
            return rc
    if args.dry_run:
        log("dry-run complete; stamp not written")
        return 0
    write_ok_stamp(stamp)
    log("wrote %s" % stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
