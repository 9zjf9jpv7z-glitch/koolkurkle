#!/usr/bin/env python3
"""Mini daily RAG orchestrator: IMAP headers → FTS → classify/bills → embed.

Wires existing MailArchive scripts. Does not reimplement IMAP, FTS, classify,
or embed. No Python IMAP sockets. No secrets. Copy-only until SoR cutover
(PR-5): MAILROOM_DB basename must be mailroom-copy.sqlite or
mailroom-daily-copy.sqlite. Unset / mailroom.sqlite / other names refuse
before IMAP or embed.

Catch-up: if last_daily_rag_ok is missing or at least 24h old, run the
chain (resume first failed phase). last_daily_rag_ok is written only after
imap + bodies + embed succeed (classify/bills may warn). Exclusive flock
on mailroom.daily.lock so calendar + RunAtLoad do not double-run.

  /usr/bin/python3 mailroom_daily.py --print-plan
  /usr/bin/python3 mailroom_daily.py --skip-if-fresh
  /usr/bin/python3 mailroom_daily.py --force
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

APPLE_CURL = "/usr/bin/curl"
APPLE_PY = "/usr/bin/python3"
DEFAULT_ARCHIVE = Path.home() / "MailArchive"
STAMP_NAME = "last_daily_rag_ok"
IMAP_STAMP = "last_imap_ok"
BODIES_STAMP = "last_bodies_ok"
EMBED_STAMP = "last_embed_ok"
LOCK_NAME = "mailroom.daily.lock"
COPY_DB_BASENAMES = frozenset(
    {
        "mailroom-copy.sqlite",
        "mailroom-daily-copy.sqlite",
    }
)
ALLOWLIST_HELP = "mailroom-copy.sqlite or mailroom-daily-copy.sqlite"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
CATCH_UP_MAX_AGE_SEC = 24 * 60 * 60
# Calendar fire can be a few minutes early vs last night's stamp.
CATCH_UP_SLOP_SEC = 15 * 60
STEP_WATERMARK = {
    "headers": IMAP_STAMP,
    "bodies-fts": BODIES_STAMP,
    "embed": EMBED_STAMP,
}
WARN_STEPS = frozenset({"classify", "bills"})
REQUIRED_STAMPS = (IMAP_STAMP, BODIES_STAMP, EMBED_STAMP)

HEADER_SCRIPTS = ("imap_newmail.py", "imap_tombstone.py")
BODY_SCRIPTS = ("imap_fetch_bodies_fts.py", "imap_fetch_bodies.py")
CLASSIFY_SCRIPT = "classify.py"
BILLS_SCRIPT = "notify_bills.py"
EMBED_SCRIPT = "embed_backfill.py"


class DailyError(RuntimeError):
    """Orchestrator failure (never includes secrets)."""


class DailyRefuse(DailyError):
    """Hard refuse (db_mode=refused). No IMAP/embed."""


class DailyLockHeld(DailyError):
    """Another daily start holds mailroom.daily.lock."""


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


@dataclass
class HeldDailyLock:
    fd: object
    path: Path


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


def default_db_path(archive: Path) -> Path | None:
    """Return MAILROOM_DB if set. Never silently default to mailroom.sqlite."""
    raw = (os.environ.get("MAILROOM_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return None


def allowed_copy_db(path: Path) -> bool:
    return path.name in COPY_DB_BASENAMES


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


def resolve_driver_db(cli_db: str | None, archive: Path) -> Path:
    """Hard-fail unless basename is on the copy allowlist. archive unused on purpose."""
    del archive
    if cli_db:
        path = Path(cli_db).expanduser()
    else:
        path = default_db_path(Path("."))
        if path is None:
            raise DailyRefuse(unset_db_message())
    if not allowed_copy_db(path):
        raise DailyRefuse(refuse_copy_db_message(path))
    return path


def emit_db_mode(mode: str) -> None:
    sys.stderr.write("db_mode=%s\n" % mode)
    sys.stderr.flush()


def default_lock_path(archive: Path) -> Path:
    raw = (os.environ.get("MAILROOM_DAILY_LOCK") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return archive / LOCK_NAME


def acquire_daily_lock(path: Path) -> "HeldDailyLock":
    """Exclusive non-blocking flock. Calendar + RunAtLoad must not double-run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise DailyLockHeld(
            "mailroom.daily.lock held; skipping this start "
            "(calendar + RunAtLoad must not double-run): %s" % path
        ) from exc
    return HeldDailyLock(fd=fh, path=path)


def release_daily_lock(held: "HeldDailyLock") -> None:
    try:
        fcntl.flock(held.fd.fileno(), fcntl.LOCK_UN)
    finally:
        held.fd.close()


def ollama_host() -> str:
    return (os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")


def embed_health_url() -> str:
    return ollama_host() + "/api/tags"


def check_embed_health(timeout: float = 5.0) -> None:
    """GET local Ollama /api/tags before incremental embed. Embed-only."""
    url = embed_health_url()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if not (200 <= int(status) < 300):
                raise DailyError(
                    "embed health-check failed: GET %s -> %s. "
                    "Local Ollama (qwen3-embedding:8b) is required."
                    % (url, status)
                )
    except DailyError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DailyError(
            "embed health-check failed: GET %s (%s). "
            "Local Ollama on 127.0.0.1:11434 is required."
            % (url, exc.__class__.__name__)
        ) from exc


def stamp_path(logs_dir: Path, name: str = STAMP_NAME) -> Path:
    return logs_dir / name


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
    """Atomic stamp: write temp then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time() if now is None else now
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    payload = when.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.utime(tmp, (ts, ts))
    tmp.replace(path)


def phase_done_this_cycle(
    phase_stamp: Path,
    daily_stamp: Path,
    now: float | None = None,
) -> bool:
    """True when this phase already succeeded in the current catch-up cycle.

    Fresh phase stamp (within 24h-slop) that is newer than last_daily_rag_ok
    (or exists when the daily stamp is missing) means resume: do not redo it.
    Stale leftover stamps from the last full success are redone.
    """
    if not phase_stamp.is_file():
        return False
    now_ts = time.time() if now is None else now
    age = stamp_age_seconds(phase_stamp, now=now_ts)
    if age is None:
        return False
    threshold = max(0, CATCH_UP_MAX_AGE_SEC - CATCH_UP_SLOP_SEC)
    if age >= threshold:
        return False
    if not daily_stamp.is_file():
        return True
    return phase_stamp.stat().st_mtime > daily_stamp.stat().st_mtime


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
            "Mini daily RAG (copy-only): headers (Apple curl) → body/FTS → "
            "classify → bills → incremental embed. MAILROOM_DB basename must "
            "be mailroom-copy.sqlite or mailroom-daily-copy.sqlite. Stamp "
            "last_daily_rag_ok only after imap+bodies+embed succeed."
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
        help=(
            "Driver DB (required: $MAILROOM_DB or --db). Basename allowlist: "
            "mailroom-copy.sqlite, mailroom-daily-copy.sqlite. Unset / "
            "mailroom.sqlite refused until SoR cutover."
        ),
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

    try:
        db = resolve_driver_db(args.db, archive)
    except DailyRefuse as exc:
        emit_db_mode("refused")
        sys.stderr.write("error: %s\n" % exc)
        return 2
    emit_db_mode("copy")

    stamp = stamp_path(logs_dir)
    held: HeldDailyLock | None = None
    if not args.print_plan:
        try:
            held = acquire_daily_lock(default_lock_path(archive))
        except DailyLockHeld as exc:
            log("skip: %s" % exc)
            return 0

    try:
        return _run_after_guard(args, archive, scripts_dir, logs_dir, db, stamp, log)
    finally:
        if held is not None:
            release_daily_lock(held)


def _run_after_guard(args, archive, scripts_dir, logs_dir, db, stamp, log) -> int:
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
    imap_stamp = stamp_path(logs_dir, IMAP_STAMP)
    bodies_stamp = stamp_path(logs_dir, BODIES_STAMP)
    embed_stamp = stamp_path(logs_dir, EMBED_STAMP)
    done = {
        IMAP_STAMP: phase_done_this_cycle(imap_stamp, stamp),
        BODIES_STAMP: phase_done_this_cycle(bodies_stamp, stamp),
        EMBED_STAMP: phase_done_this_cycle(embed_stamp, stamp),
    }

    i = 0
    while i < len(items):
        step = items[i].step
        group = []
        while i < len(items) and items[i].step == step:
            group.append(items[i])
            i += 1
        watermark = STEP_WATERMARK.get(step)
        warn_only = step in WARN_STEPS
        if watermark and done.get(watermark):
            log("skip %s (watermark %s)" % (step, watermark))
            continue
        if (
            warn_only
            and done[IMAP_STAMP]
            and done[BODIES_STAMP]
            and done[EMBED_STAMP]
        ):
            log("skip %s (required watermarks already set)" % step)
            continue
        if step == "embed" and not args.dry_run:
            try:
                check_embed_health()
            except DailyError as exc:
                log("error: %s" % exc)
                log("chain aborted; not writing %s" % STAMP_NAME)
                return 2
        step_rc = 0
        for item in group:
            rc = run_step(item, dry_run=args.dry_run, log=log)
            if rc != 0:
                step_rc = rc
                if warn_only:
                    log(
                        "warning: %s exited %s (classify may warn; continuing)"
                        % (item.step, rc)
                    )
                    continue
                log("chain aborted; not writing %s" % STAMP_NAME)
                return rc
        if args.dry_run:
            continue
        if watermark and step_rc == 0:
            dest = stamp_path(logs_dir, watermark)
            write_ok_stamp(dest)
            done[watermark] = True
            log("wrote %s" % dest)

    if args.dry_run:
        log("dry-run complete; stamp not written")
        return 0
    if all(done[name] for name in REQUIRED_STAMPS):
        write_ok_stamp(stamp)
        log("wrote %s" % stamp)
        return 0
    log("required phases incomplete; not writing %s" % STAMP_NAME)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
