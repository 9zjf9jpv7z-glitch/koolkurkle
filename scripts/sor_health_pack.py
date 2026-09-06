#!/usr/bin/env python3
"""Read-only SoR health + FTS/hybrid smoke for mailroom.sqlite.

Opens ``$MAILROOM_DB`` or ``$HOME/MailArchive/mailroom.sqlite`` (Path.home /
expanduser — no machine home hardcodes). Prints counts, integrity, FTS hit
counts / top subjects, and hybrid ``retrieve()`` vec_rank presence. Never
prints bodies, Keychain values, or app passwords. Does not kill writers.

Exit: 0 ok-or-warnings, 1 integrity not ok, 2 missing/unreadable DB.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import embed_lib as el  # noqa: E402
import semantic_search as ss  # noqa: E402

FTS_QUERIES = ("bill", "Caddell")
HYBRID_QUERIES = ("SDGE bill", "Caddell")
SMOKE_K = 5
SUBJECT_MAX = 120
OLLAMA_PROBE_TIMEOUT = 2.0
MISSING_RANK = ss.MISSING_RANK

# Process / LaunchAgent needles for embed backfill — report only, never kill.
PROCESS_NEEDLES = (
    "embed_backfill",
    "embed-rem",
    "embed_rem",
    "id-rem",
)
LAUNCHAGENT_NEEDLES = (
    "embed-rem",
    "embed_rem",
    "embed-shard",
    "embed_shard",
    "id-rem",
    "mailroom.embed",
)

_SECRETISH = re.compile(
    r"(?i)(password|passwd|secret|token|keychain|IMAP_APP_PASSWORD|"
    r"BEGIN [A-Z0-9 ]{0,24}PRIVATE|"
    r"(--?(?:password|secret|token)=\S+))"
)


class HealthError(RuntimeError):
    """SoR health failure (never includes secrets)."""


RetrieveFn = Callable[..., list[dict[str, Any]]]
EmbedProbeFn = Callable[[str], tuple[Any, str | None]]
WriterScanFn = Callable[[], list["WriterHit"]]


@dataclass
class FtsSmoke:
    query: str
    present: bool
    hits: int | None
    subjects: list[str]


@dataclass
class HybridSmoke:
    query: str
    hits: int
    vec_real: int
    vec_missing: int
    subjects: list[str]
    fail_open: bool
    detail: str | None


@dataclass
class WriterHit:
    kind: str
    pid: str | None
    label: str | None
    detail: str


@dataclass
class HealthReport:
    db: str
    opened: bool
    integrity_ok: bool
    integrity: str
    messages: int | None
    embeddings: int | None
    embedding_meta: int | None
    coverage_gap: int | None
    fts_present: bool
    fts: list[FtsSmoke] = field(default_factory=list)
    hybrid: list[HybridSmoke] = field(default_factory=list)
    backups_present: bool | None = None
    backups_path: str = ""
    writers: list[WriterHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def exit_code(self) -> int:
        if not self.opened:
            return 2
        if not self.integrity_ok:
            return 1
        return 0


def default_db_path() -> Path:
    raw = (os.environ.get("MAILROOM_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "MailArchive" / "mailroom.sqlite"


def default_backups_path() -> Path:
    return Path.home() / "MailArchive" / "backups"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return ss.table_exists(conn, name)


def open_sor(
    path: Path, extension_path: str | None = None
) -> sqlite3.Connection:
    """Prefer embed_lib.connect_db (sqlite-vec); fall back to plain sqlite3."""
    if not path.is_file():
        raise HealthError("database not found: %s" % path)
    try:
        return el.connect_db(path, extension_path)
    except Exception:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


def check_integrity(conn: sqlite3.Connection) -> tuple[bool, str]:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if not rows:
        return False, "empty"
    text = str(rows[0][0] if not isinstance(rows[0], sqlite3.Row) else rows[0][0])
    return text.strip().lower() == "ok", text.strip()


def _count(conn: sqlite3.Connection, sql: str) -> int | None:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def collect_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    messages = _count(conn, "SELECT COUNT(*) FROM messages") if table_exists(
        conn, "messages"
    ) else None
    embeddings = None
    if table_exists(conn, "message_embeddings"):
        embeddings = _count(conn, "SELECT COUNT(*) FROM message_embeddings")
    embedding_meta = None
    if table_exists(conn, "embedding_meta"):
        embedding_meta = _count(
            conn, "SELECT COUNT(DISTINCT message_id) FROM embedding_meta"
        )
    fts_present = table_exists(conn, "messages_fts")
    coverage_gap = None
    if messages is not None and table_exists(conn, "messages"):
        if table_exists(conn, "embedding_meta"):
            coverage_gap = _count(
                conn,
                "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS ("
                "SELECT 1 FROM embedding_meta e WHERE e.message_id = m.id)",
            )
        elif table_exists(conn, "message_embeddings"):
            coverage_gap = _count(
                conn,
                "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS ("
                "SELECT 1 FROM message_embeddings v WHERE v.message_id = m.id)",
            )
    return {
        "messages": messages,
        "embeddings": embeddings,
        "embedding_meta": embedding_meta,
        "coverage_gap": coverage_gap,
        "fts_present": fts_present,
    }


def _clip_subject(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > SUBJECT_MAX:
        return text[: SUBJECT_MAX - 1] + "…"
    return text


def _fts_hit_count(conn: sqlite3.Connection, query: str) -> int | None:
    if not table_exists(conn, "messages_fts"):
        return None
    match_q = ss.fts_match_query(query)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (match_q,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else 0


def fts_smoke(conn: sqlite3.Connection, query: str, *, k: int = SMOKE_K) -> FtsSmoke:
    present = table_exists(conn, "messages_fts")
    if not present:
        return FtsSmoke(query=query, present=False, hits=None, subjects=[])
    hits = _fts_hit_count(conn, query)
    subjects: list[str] = []
    try:
        ranked = ss.fts_search(conn, query, k=k)
    except sqlite3.Error:
        ranked = []
    for item in ranked:
        mid = str(item.get("message_id") or "")
        if not mid:
            continue
        meta = ss._load_message(conn, mid)
        subjects.append(_clip_subject(meta.get("subject")))
    return FtsSmoke(query=query, present=True, hits=hits, subjects=subjects)


def is_real_vec_rank(rank: Any) -> bool:
    if rank is None:
        return False
    try:
        value = int(rank)
    except (TypeError, ValueError):
        return False
    return 0 < value < int(MISSING_RANK)


def ollama_reachable(
    ollama_url: str = el.DEFAULT_OLLAMA_URL,
    timeout: float = OLLAMA_PROBE_TIMEOUT,
) -> bool:
    url = ollama_url.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= int(status) < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def embeddings_exist(counts: dict[str, Any]) -> bool:
    for key in ("embeddings", "embedding_meta"):
        value = counts.get(key)
        if isinstance(value, int) and value > 0:
            return True
    return False


def hybrid_smoke(
    query: str,
    *,
    db: Path,
    conn: sqlite3.Connection,
    counts: dict[str, Any],
    k: int = SMOKE_K,
    retrieve_fn: RetrieveFn | None = None,
    embed_probe_fn: EmbedProbeFn | None = None,
    ollama_url: str = el.DEFAULT_OLLAMA_URL,
    ollama_up: bool | None = None,
) -> HybridSmoke:
    """Call semantic_search.retrieve(); report real vec_rank vs missing/1000."""
    worker = retrieve_fn or ss.retrieve
    has_vec = embeddings_exist(counts)
    fail_open = False
    detail: str | None = None
    query_vector = None
    embed_fn = None

    if ollama_up is False:
        fail_open = True
        detail = "Ollama/embed unavailable — fail-open (vec_rank missing/1000)"

        def _down(_texts: Any, _model: str) -> list[list[float]]:
            raise RuntimeError("Ollama/embed unavailable")

        embed_fn = _down
    elif embed_probe_fn is not None:
        query_vector, warn = embed_probe_fn(query)
        if query_vector is None:
            fail_open = True
            detail = warn or "Ollama/embed unavailable — fail-open (vec_rank missing/1000)"

    try:
        hits = worker(
            query,
            k=k,
            db=db,
            conn=conn,
            embed_fn=embed_fn,
            query_vector=query_vector,
            ollama_url=ollama_url,
            expand_threads=False,
            rerank=False,
        )
    except Exception as exc:  # fail-open hybrid — integrity still reported
        return HybridSmoke(
            query=query,
            hits=0,
            vec_real=0,
            vec_missing=0,
            subjects=[],
            fail_open=True,
            detail="retrieve failed (%s)" % exc,
        )

    rows = list(hits or [])
    real = sum(1 for h in rows if is_real_vec_rank(h.get("vec_rank")))
    missing = len(rows) - real
    subjects = [_clip_subject(h.get("subject")) for h in rows[:k]]
    if has_vec and rows and real == 0 and not fail_open:
        detail = (
            "embeddings present but no real vec_rank "
            "(all missing/%s)" % MISSING_RANK
        )
    elif has_vec and not rows and not fail_open:
        detail = "embeddings present but retrieve returned no hits"
    return HybridSmoke(
        query=query,
        hits=len(rows),
        vec_real=real,
        vec_missing=missing,
        subjects=subjects,
        fail_open=fail_open,
        detail=detail,
    )


def _safe_detail(text: str) -> str:
    clipped = " ".join(str(text or "").split())
    if _SECRETISH.search(clipped):
        return "<redacted>"
    return clipped[:160]


def _parse_ps_lines(blob: str) -> list[WriterHit]:
    hits: list[WriterHit] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or "sor_health_pack" in line:
            continue
        lower = line.lower()
        if not any(needle in lower for needle in PROCESS_NEEDLES):
            continue
        parts = line.split(None, 1)
        pid = parts[0] if parts and parts[0].isdigit() else None
        cmd = parts[1] if len(parts) > 1 else line
        hits.append(
            WriterHit(kind="process", pid=pid, label=None, detail=_safe_detail(cmd))
        )
    return hits


def _parse_launchctl_lines(blob: str) -> list[WriterHit]:
    hits: list[WriterHit] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if not any(needle in lower for needle in LAUNCHAGENT_NEEDLES):
            continue
        # launchctl list: PID  Status  Label
        parts = line.split()
        pid = None
        label = parts[-1] if parts else line
        if parts and parts[0] not in ("-", "PID") and parts[0].isdigit():
            pid = parts[0]
        if label == "Label":
            continue
        hits.append(
            WriterHit(
                kind="launchctl",
                pid=pid,
                label=label,
                detail=_safe_detail(line),
            )
        )
    return hits


def scan_embed_writers(
    *,
    home: Path | None = None,
    run: Callable[[list[str]], str | None] | None = None,
) -> list[WriterHit]:
    """Find embed_backfill / rem LaunchAgent patterns. Never kill."""
    runner = run or _run_capture
    found: list[WriterHit] = []
    for cmd in (
        ["ps", "-ax", "-o", "pid=", "-o", "command="],
        ["ps", "-eo", "pid=", "-o", "args="],
    ):
        blob = runner(cmd)
        if blob:
            found.extend(_parse_ps_lines(blob))
            break
    launch = runner(["launchctl", "list"])
    if launch:
        found.extend(_parse_launchctl_lines(launch))
    root = home if home is not None else Path.home()
    agents = root / "Library" / "LaunchAgents"
    if agents.is_dir():
        for plist in sorted(agents.glob("*.plist")):
            name = plist.name.lower()
            if any(needle in name for needle in LAUNCHAGENT_NEEDLES):
                found.append(
                    WriterHit(
                        kind="plist",
                        pid=None,
                        label=plist.stem,
                        detail=plist.name,
                    )
                )
    return found


def _run_capture(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return None
    return proc.stdout or ""


def check_backups(path: Path | None = None) -> tuple[Path, bool]:
    target = path if path is not None else default_backups_path()
    return target, target.exists()


def run_health(
    db: Path | None = None,
    *,
    extension_path: str | None = None,
    skip_hybrid: bool = False,
    skip_writers: bool = False,
    k: int = SMOKE_K,
    retrieve_fn: RetrieveFn | None = None,
    embed_probe_fn: EmbedProbeFn | None = None,
    writer_scan_fn: WriterScanFn | None = None,
    backups_path: Path | None = None,
    ollama_url: str = el.DEFAULT_OLLAMA_URL,
    ollama_up: bool | None = None,
    fts_queries: Iterable[str] = FTS_QUERIES,
    hybrid_queries: Iterable[str] = HYBRID_QUERIES,
) -> HealthReport:
    path = db if db is not None else default_db_path()
    backups, backups_present = check_backups(backups_path)
    report = HealthReport(
        db=str(path),
        opened=False,
        integrity_ok=False,
        integrity="not-run",
        messages=None,
        embeddings=None,
        embedding_meta=None,
        coverage_gap=None,
        fts_present=False,
        backups_present=backups_present,
        backups_path=str(backups),
    )
    if not backups_present:
        report.warnings.append("backups path missing: %s" % backups)

    try:
        conn = open_sor(path, extension_path)
    except HealthError as exc:
        report.errors.append(str(exc))
        return report
    except sqlite3.Error as exc:
        report.errors.append("cannot open database: %s" % exc)
        return report

    report.opened = True
    try:
        ok, raw = check_integrity(conn)
        report.integrity_ok = ok
        report.integrity = raw
        if not ok:
            report.errors.append("integrity_check not ok")

        counts = collect_counts(conn)
        report.messages = counts["messages"]
        report.embeddings = counts["embeddings"]
        report.embedding_meta = counts["embedding_meta"]
        report.coverage_gap = counts["coverage_gap"]
        report.fts_present = bool(counts["fts_present"])

        if not report.fts_present:
            report.warnings.append("FTS missing (no messages_fts)")
        if not embeddings_exist(counts):
            report.warnings.append("no vec / embeddings")
        gap = report.coverage_gap
        if isinstance(gap, int) and gap > 0:
            report.warnings.append("coverage gap: %s messages without embeddings" % gap)

        for query in fts_queries:
            report.fts.append(fts_smoke(conn, query, k=k))

        if skip_hybrid:
            report.warnings.append("hybrid smoke skipped")
        else:
            up = ollama_up
            if up is None and embed_probe_fn is None:
                up = ollama_reachable(ollama_url)
                if not up:
                    report.warnings.append(
                        "Ollama/embed unavailable — hybrid fail-open"
                    )
            for query in hybrid_queries:
                smoke = hybrid_smoke(
                    query,
                    db=path,
                    conn=conn,
                    counts=counts,
                    k=k,
                    retrieve_fn=retrieve_fn,
                    embed_probe_fn=embed_probe_fn,
                    ollama_url=ollama_url,
                    ollama_up=up,
                )
                report.hybrid.append(smoke)
                if smoke.fail_open and smoke.detail:
                    if smoke.detail not in report.warnings:
                        report.warnings.append(smoke.detail)
                elif smoke.detail and embeddings_exist(counts):
                    report.warnings.append("%s: %s" % (query, smoke.detail))

        if skip_writers:
            report.warnings.append("embed-writer scan skipped")
        else:
            scanner = writer_scan_fn or scan_embed_writers
            report.writers = list(scanner())
    finally:
        conn.close()
    return report


def format_report(report: HealthReport) -> str:
    lines = [
        "SoR: %s" % report.db,
        "integrity: %s" % ("ok" if report.integrity_ok else "FAIL"),
        "counts: messages=%s embeddings=%s embedding_meta=%s coverage_gap=%s fts=%s"
        % (
            report.messages,
            report.embeddings,
            report.embedding_meta,
            report.coverage_gap,
            "yes" if report.fts_present else "no",
        ),
    ]
    for item in report.fts:
        if not item.present:
            lines.append("fts %r: missing" % item.query)
            continue
        lines.append("fts %r: hits=%s" % (item.query, item.hits))
        for subject in item.subjects:
            lines.append("  subject: %s" % subject)
    for item in report.hybrid:
        flag = " fail-open" if item.fail_open else ""
        lines.append(
            "hybrid %r: hits=%s vec_real=%s vec_missing=%s%s"
            % (item.query, item.hits, item.vec_real, item.vec_missing, flag)
        )
        if item.detail:
            lines.append("  note: %s" % item.detail)
        for subject in item.subjects:
            lines.append("  subject: %s" % subject)
    lines.append(
        "backups: %s (%s)"
        % (
            "present" if report.backups_present else "missing",
            report.backups_path,
        )
    )
    if report.writers:
        lines.append("embed writers: %s (not killed)" % len(report.writers))
        for hit in report.writers:
            lines.append(
                "  %s pid=%s label=%s %s"
                % (hit.kind, hit.pid or "-", hit.label or "-", hit.detail)
            )
    else:
        lines.append("embed writers: none found")
    if report.warnings:
        lines.append("warnings:")
        for warn in report.warnings:
            lines.append("  - %s" % warn)
    if report.errors:
        lines.append("errors:")
        for err in report.errors:
            lines.append("  - %s" % err)
    status = "OK" if report.exit_code() == 0 else "FAIL"
    lines.append("status: %s" % status)
    return "\n".join(lines)


def report_to_json(report: HealthReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["exit_code"] = report.exit_code()
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only SoR integrity + FTS/hybrid smoke. "
            "Default DB: $MAILROOM_DB or $HOME/MailArchive/mailroom.sqlite."
        )
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SoR path (default: $MAILROOM_DB or $HOME/MailArchive/mailroom.sqlite).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=SMOKE_K,
        help="Top subjects per smoke query (default: %s)." % SMOKE_K,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object (subjects only, no bodies).",
    )
    parser.add_argument(
        "--skip-hybrid",
        action="store_true",
        help="Skip retrieve()/Ollama hybrid smoke.",
    )
    parser.add_argument(
        "--skip-writers",
        action="store_true",
        help="Skip embed_backfill / LaunchAgent presence scan.",
    )
    parser.add_argument(
        "--vec-extension",
        default=None,
        help="Optional vec0 dylib/so for embed_lib.connect_db.",
    )
    parser.add_argument(
        "--ollama-url",
        default=el.DEFAULT_OLLAMA_URL,
        help="Local Ollama base URL (default: %s)." % el.DEFAULT_OLLAMA_URL,
    )
    parser.add_argument(
        "--backups",
        default=None,
        help="Backup dir to test for existence (default: $HOME/MailArchive/backups).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db).expanduser() if args.db else default_db_path()
    backups = Path(args.backups).expanduser() if args.backups else None
    report = run_health(
        db,
        extension_path=args.vec_extension,
        skip_hybrid=bool(args.skip_hybrid),
        skip_writers=bool(args.skip_writers),
        k=max(1, int(args.k)),
        backups_path=backups,
        ollama_url=args.ollama_url,
    )
    if args.json:
        sys.stdout.write(
            json.dumps(report_to_json(report), ensure_ascii=False, indent=2) + "\n"
        )
    else:
        sys.stdout.write(format_report(report) + "\n")
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
