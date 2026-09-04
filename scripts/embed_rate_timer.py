#!/usr/bin/python3
"""Digital-timer scoreboard for MailArchive Ollama embed backfill rates.

Stdlib only (http.server + HTML/JS). Do not import tkinter — system Tk on
mac-mini.local aborts with a false "macOS 26 required" check.

Modes
  A  Local browser UI for this Mac's newest embed log.
  B  Dual pane: this process serves HTML + /status.json, and optionally
     fetches the other Mac's /status.json (--mini-url / --mbp-url / --dual).

Apple CLT /usr/bin/python3 (3.9+) or Homebrew python3.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 17854
DEFAULT_WINDOW_SEC = 180  # rolling ~3 min (allowed 120–300)
DEFAULT_STALE_SEC = 90
DEFAULT_POLL_MS = 1000
DEFAULT_LOGS_DIR = os.path.expanduser("~/MailArchive/logs")
DEFAULT_MINI_HOST = "mac-mini.local"
TAIL_BYTES = 512 * 1024
MIN_RATE_SPAN_SEC = 20.0
SAMPLE_KEEP = 4000

COMMITTED_RE = re.compile(r"committed\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
LINE_TS_RE = re.compile(
    r"^\s*[\[(<]?(?P<ts>\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)
FILENAME_TS_RE = re.compile(r"(?<!\d)(\d{8})_(\d{6})(?!\d)")
DRYRUN_RE = re.compile(r"dryrun", re.IGNORECASE)

ROLE_GLOBS = {
    "mbp": ("embed_full_*.log", "embed*.log"),
    "mini": ("embed_shard1_*.log", "embed*.log"),
}

# ---------------------------------------------------------------------------
# Role / log discovery
# ---------------------------------------------------------------------------


def detect_role(hostname: Optional[str] = None) -> str:
    host = (hostname or socket.gethostname() or "").lower()
    if "mini" in host:
        return "mini"
    return "mbp"


def role_label(role: str) -> str:
    return "Mini" if role == "mini" else "MBP"


def _is_dryrun(name: str) -> bool:
    return bool(DRYRUN_RE.search(os.path.basename(name)))


def _iter_glob(directory: str, pattern: str) -> List[str]:
    """fnmatch files in directory; skip dryrun. Newest mtime first."""
    import fnmatch

    if not os.path.isdir(directory):
        return []
    out: List[str] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if not fnmatch.fnmatch(name, pattern):
            continue
        if _is_dryrun(name):
            continue
        if not name.endswith(".log"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            out.append(path)
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def find_log(
    logs_dir: str,
    role: str,
    explicit: Optional[str] = None,
) -> Optional[str]:
    if explicit:
        path = os.path.expanduser(explicit)
        return path if os.path.isfile(path) else None
    logs_dir = os.path.expanduser(logs_dir)
    patterns = ROLE_GLOBS.get(role, ROLE_GLOBS["mbp"])
    for pattern in patterns:
        matches = _iter_glob(logs_dir, pattern)
        if matches:
            return matches[0]
    return None


def filename_start_ts(path: str) -> Optional[float]:
    m = FILENAME_TS_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.timestamp()
    except ValueError:
        return None


def file_birth_ts(path: str) -> Optional[float]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", None)
    if birth:
        return float(birth)
    return float(st.st_ctime)


# ---------------------------------------------------------------------------
# Log parsing + rate
# ---------------------------------------------------------------------------


def _parse_line_ts(stamp: str) -> Optional[float]:
    stamp = stamp.replace(",", ".")
    if stamp.endswith("Z"):
        stamp = stamp[:-1]
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(stamp, fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_committed_line(line: str) -> Optional[Tuple[Optional[float], int, int]]:
    cm = COMMITTED_RE.search(line)
    if not cm:
        return None
    committed = int(cm.group(1))
    total = int(cm.group(2))
    tm = LINE_TS_RE.search(line)
    ts = _parse_line_ts(tm.group("ts")) if tm else None
    return ts, committed, total


def tail_text(path: str, nbytes: int = TAIL_BYTES) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - nbytes), os.SEEK_SET)
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def parse_log_samples(text: str) -> Tuple[List[Tuple[float, int]], Optional[int], Optional[int]]:
    """Return (timestamped samples, latest committed, latest total)."""
    samples: List[Tuple[float, int]] = []
    latest_c: Optional[int] = None
    latest_t: Optional[int] = None
    for raw in text.splitlines():
        parsed = parse_committed_line(raw)
        if not parsed:
            continue
        ts, committed, total = parsed
        latest_c, latest_t = committed, total
        if ts is not None:
            samples.append((ts, committed))
    return samples, latest_c, latest_t


def compute_rate(
    samples: List[Tuple[float, int]],
    window_sec: float,
    now: Optional[float] = None,
    min_span: float = MIN_RATE_SPAN_SEC,
) -> Tuple[Optional[float], bool]:
    """Rolling rate (items/hour). Returns (rate, warming)."""
    if len(samples) < 2:
        return None, True
    samples = sorted(samples, key=lambda s: s[0])
    now_ts = now if now is not None else samples[-1][0]
    cutoff = now_ts - window_sec
    window = [s for s in samples if s[0] >= cutoff]
    before = [s for s in samples if s[0] < cutoff]
    if before and window:
        window = [before[-1]] + window
    elif len(window) < 2:
        window = samples
    if len(window) < 2:
        return None, True
    dt = window[-1][0] - window[0][0]
    if dt < min_span:
        return None, True
    dc = window[-1][1] - window[0][1]
    if dc < 0:
        return None, True
    return (dc / dt) * 3600.0, False


def overall_rate(committed: Optional[int], start_ts: Optional[float], now: float) -> Optional[float]:
    if committed is None or start_ts is None:
        return None
    dt = now - start_ts
    if dt < MIN_RATE_SPAN_SEC:
        return None
    if committed < 0:
        return None
    return (committed / dt) * 3600.0


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    sec = int(round(seconds))
    if sec < 60:
        return "%ds" % sec
    minutes, rem_s = divmod(sec, 60)
    if minutes < 60:
        if rem_s >= 30:
            return "%dm %ds" % (minutes, rem_s)
        return "%dm" % minutes
    hours, rem_m = divmod(minutes, 60)
    if hours < 48:
        return "%dh %dm" % (hours, rem_m)
    days, rem_h = divmod(hours, 24)
    return "%dd %dh" % (days, rem_h)


def format_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "—"
    if rate >= 100:
        return "{:,}".format(int(round(rate)))
    if rate >= 10:
        return "{:.0f}".format(rate)
    return "{:.1f}".format(rate)


def iso_local(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class RateTracker:
    """In-process poll samples, merged with timestamped log lines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._polls: Deque[Tuple[float, int]] = deque(maxlen=SAMPLE_KEEP)
        self._last_inode: Optional[int] = None
        self._last_committed: Optional[int] = None

    def reset(self) -> None:
        with self._lock:
            self._polls.clear()
            self._last_inode = None
            self._last_committed = None

    def note(self, inode: Optional[int], committed: int, now: float) -> None:
        with self._lock:
            if inode is not None and self._last_inode is not None and inode != self._last_inode:
                self._polls.clear()
            if self._last_committed is not None and committed < self._last_committed:
                self._polls.clear()
            self._last_inode = inode
            self._last_committed = committed
            self._polls.append((now, committed))

    def polls(self) -> List[Tuple[float, int]]:
        with self._lock:
            return list(self._polls)


def snapshot_log(
    path: Optional[str],
    role: str,
    label: str,
    hostname: str,
    window_sec: int,
    stale_sec: int,
    tracker: Optional[RateTracker] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    now_ts = time.time() if now is None else now
    base: Dict[str, Any] = {
        "ok": False,
        "role": role,
        "label": label,
        "hostname": hostname,
        "log_path": path,
        "log_mtime": None,
        "committed": None,
        "total": None,
        "remaining": None,
        "rate_per_hour": None,
        "rate_display": "—",
        "rate_kind": None,
        "window_sec": window_sec,
        "eta_seconds": None,
        "eta_human": "—",
        "stale": False,
        "done": False,
        "warming": True,
        "error": None,
        "as_of": iso_local(now_ts),
        "as_of_unix": now_ts,
    }
    if not path:
        base["error"] = "no embed log found (skip *dryrun*; newest mtime wins)"
        return base
    if not os.path.isfile(path):
        base["error"] = "log file missing: %s" % path
        return base

    try:
        st = os.stat(path)
        text = tail_text(path)
    except OSError as exc:
        base["error"] = "cannot read log: %s" % exc
        return base

    samples, committed, total = parse_log_samples(text)
    if tracker is not None and committed is not None:
        tracker.note(getattr(st, "st_ino", None), committed, now_ts)
        samples = samples + tracker.polls()

    rate, warming = compute_rate(samples, window_sec, now=now_ts)
    rate_kind = "rolling" if rate is not None else None
    if rate is None:
        start = filename_start_ts(path) or file_birth_ts(path)
        rate = overall_rate(committed, start, now_ts)
        if rate is not None:
            rate_kind = "overall"
            warming = False

    remaining = None
    eta_sec = None
    done = False
    if committed is not None and total is not None:
        remaining = max(0, total - committed)
        done = total > 0 and committed >= total
        if done:
            eta_sec = 0.0
            warming = False
        elif rate is not None and rate > 0 and remaining > 0:
            eta_sec = remaining / (rate / 3600.0)

    stale = (now_ts - st.st_mtime) > stale_sec and not done
    if committed is None:
        base["error"] = "no 'committed N/M' lines in log tail"
        base["log_mtime"] = iso_local(st.st_mtime)
        return base

    base.update(
        {
            "ok": True,
            "log_mtime": iso_local(st.st_mtime),
            "committed": committed,
            "total": total,
            "remaining": remaining,
            "rate_per_hour": None if rate is None else round(rate, 2),
            "rate_display": format_rate(rate),
            "rate_kind": rate_kind,
            "eta_seconds": None if eta_sec is None else int(round(eta_sec)),
            "eta_human": format_eta(eta_sec),
            "stale": stale,
            "done": done,
            "warming": warming and not done,
            "error": None,
        }
    )
    return base


def fetch_remote_status(url: str, timeout: float = 1.5) -> Dict[str, Any]:
    url = normalize_status_url(url)
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "embed-rate-timer/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("status.json is not an object")
        data.setdefault("ok", True)
        return data
    except (URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "role": None,
            "label": None,
            "error": "unreachable: %s" % exc,
            "remote_url": url,
            "rate_display": "—",
            "eta_human": "—",
            "warming": False,
            "stale": True,
            "done": False,
        }


def normalize_status_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/status.json") or url.endswith("/api/local"):
        return url
    return url + "/status.json"


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EMBED RATE</title>
<style>
  :root {
    --bg: #07080b;
    --panel: #0c0e13;
    --line: #1c2230;
    --amber: #ffb020;
    --amber-dim: #7a5410;
    --mint: #3ee9a8;
    --mint-dim: #1a5c44;
    --red: #ff5a5a;
    --muted: #6b7385;
    --soft: #c8cdd8;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%;
    background: radial-gradient(1200px 700px at 50% -10%, #141824 0%, var(--bg) 55%);
    color: var(--soft);
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }
  body { display: flex; flex-direction: column; min-height: 100%; }
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 18px 28px 8px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--muted);
    font-size: 12px;
  }
  header .title { color: #9aa3b5; }
  header .clock { letter-spacing: 0.12em; }
  main {
    flex: 1;
    display: grid;
    grid-template-columns: 1fr;
    gap: 20px;
    padding: 8px 24px 28px;
    align-items: stretch;
  }
  main.dual { grid-template-columns: 1fr 1fr; }
  .pane {
    background: linear-gradient(180deg, #10131b 0%, var(--panel) 100%);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 28px 28px 22px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
  }
  .who {
    display: flex; justify-content: space-between; align-items: baseline;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    font-size: 15px;
    color: var(--amber);
  }
  .who .host { letter-spacing: 0.06em; color: var(--muted); font-size: 11px; text-transform: none; }
  .rate-wrap {
    flex: 1;
    display: flex;
    align-items: baseline;
    justify-content: center;
    gap: 16px;
    padding: 18px 0 8px;
  }
  .rate {
    font-size: clamp(5.2rem, 14vw, 11rem);
    line-height: 0.9;
    font-weight: 600;
    letter-spacing: 0.02em;
    font-variant-numeric: tabular-nums;
    color: var(--amber);
    text-shadow: 0 0 22px rgba(255,176,32,0.28), 0 0 2px rgba(255,176,32,0.6);
  }
  .unit {
    font-size: clamp(1.1rem, 2.4vw, 2rem);
    color: var(--amber-dim);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    padding-bottom: 0.35em;
  }
  .meta {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
    border-top: 1px solid var(--line);
    padding-top: 16px;
    margin-top: 4px;
  }
  .meta .cell { min-width: 0; }
  .k {
    font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase;
    color: var(--muted); margin-bottom: 6px;
  }
  .v {
    font-size: clamp(1.05rem, 2vw, 1.55rem);
    color: var(--soft);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .track {
    height: 3px; background: #171b24; border-radius: 99px; margin: 16px 0 0;
    overflow: hidden;
  }
  .track > i {
    display: block; height: 100%; width: 0;
    background: linear-gradient(90deg, var(--amber-dim), var(--amber));
  }
  .status {
    margin-top: 12px; font-size: 11px; letter-spacing: 0.16em;
    text-transform: uppercase; color: var(--muted);
    min-height: 1.2em;
  }
  .status.live { color: var(--mint); }
  .status.stale { color: var(--amber); }
  .status.err { color: var(--red); }
  .status.done { color: var(--mint); }
  .hint { color: var(--muted); font-size: 12px; letter-spacing: 0.04em; margin-top: 8px; }
  footer {
    padding: 0 28px 16px;
    color: #4b5363;
    font-size: 11px;
    letter-spacing: 0.08em;
  }
  @media (max-width: 900px) {
    main.dual { grid-template-columns: 1fr; }
    .rate { font-size: clamp(4rem, 18vw, 7rem); }
  }
</style>
</head>
<body>
<header>
  <div class="title">Embed rate · MailArchive</div>
  <div class="clock" id="clock">--:--:--</div>
</header>
<main id="board"></main>
<footer id="foot"></footer>
<script>
const POLL_MS = __POLL_MS__;
function $(id) { return document.getElementById(id); }
function fmtInt(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("en-US");
}
function pct(c, t) {
  if (!t || c === null || c === undefined) return 0;
  return Math.max(0, Math.min(100, (c / t) * 100));
}
function statusOf(p) {
  if (!p) return { cls: "err", text: "no data" };
  if (p.error && !p.ok) return { cls: "err", text: p.error };
  if (p.done) return { cls: "done", text: "complete" };
  if (p.stale) return { cls: "stale", text: "stale · log not advancing" };
  if (p.warming) return { cls: "stale", text: "warming · collecting window" };
  const kind = p.rate_kind === "overall" ? "overall" : ((p.window_sec || 180) + "s window");
  return { cls: "live", text: "live · " + kind };
}
function pane(p, fallbackLabel) {
  const label = (p && p.label) || fallbackLabel || "—";
  const host = (p && (p.hostname || "")) || "";
  const rate = (p && p.rate_display) || "—";
  const committed = p ? p.committed : null;
  const total = p ? p.total : null;
  const remaining = p ? p.remaining : null;
  const eta = (p && p.eta_human) || "—";
  const st = statusOf(p);
  const width = pct(committed, total).toFixed(1);
  const errHint = (p && p.error && p.remote_url)
    ? `<div class="hint">Firewall / Little Snitch may be blocking ${p.remote_url}. Open that Mac’s UI in another tab, or allow inbound on the status port.</div>`
    : "";
  return `<section class="pane">
    <div class="who"><span>${label}</span><span class="host">${host}</span></div>
    <div class="rate-wrap">
      <div class="rate">${rate}</div>
      <div class="unit">/ hr</div>
    </div>
    <div class="meta">
      <div class="cell"><div class="k">Committed</div><div class="v">${fmtInt(committed)} / ${fmtInt(total)}</div></div>
      <div class="cell"><div class="k">Remaining</div><div class="v">${fmtInt(remaining)}</div></div>
      <div class="cell"><div class="k">ETA</div><div class="v">${eta}</div></div>
    </div>
    <div class="track"><i style="width:${width}%"></i></div>
    <div class="status ${st.cls}">${st.text}</div>
    ${errHint}
  </section>`;
}
function tickClock() {
  const d = new Date();
  $("clock").textContent = d.toLocaleTimeString("en-GB", { hour12: false });
}
async function refresh() {
  tickClock();
  try {
    const r = await fetch("/api/board", { cache: "no-store" });
    const data = await r.json();
    const dual = !!(data.dual && data.mbp && data.mini);
    const board = $("board");
    board.className = dual ? "dual" : "";
    if (dual) {
      board.innerHTML = pane(data.mbp, "MBP") + pane(data.mini, "Mini");
    } else {
      const local = data.local || {};
      board.innerHTML = pane(local, local.label || "Local");
    }
    const bits = [];
    if (data.local && data.local.log_path) bits.push(data.local.log_path);
    if (data.remote_url) bits.push("remote " + data.remote_url);
    $("foot").textContent = bits.join("  ·  ");
  } catch (err) {
    $("foot").textContent = "poll failed: " + err;
  }
}
refresh();
setInterval(refresh, POLL_MS);
setInterval(tickClock, 250);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class BoardState:
    def __init__(
        self,
        logs_dir: str,
        role: str,
        label: str,
        log_path: Optional[str],
        window_sec: int,
        stale_sec: int,
        poll_ms: int,
        mini_url: Optional[str],
        mbp_url: Optional[str],
        dual: bool,
    ) -> None:
        self.logs_dir = logs_dir
        self.role = role
        self.label = label
        self.explicit_log = log_path
        self.window_sec = window_sec
        self.stale_sec = stale_sec
        self.poll_ms = poll_ms
        self.mini_url = mini_url
        self.mbp_url = mbp_url
        self.dual = dual
        self.hostname = socket.gethostname()
        self.tracker = RateTracker()

    def local_status(self) -> Dict[str, Any]:
        path = find_log(self.logs_dir, self.role, self.explicit_log)
        return snapshot_log(
            path,
            self.role,
            self.label,
            self.hostname,
            self.window_sec,
            self.stale_sec,
            tracker=self.tracker,
        )

    def board(self) -> Dict[str, Any]:
        local = self.local_status()
        remote = None
        remote_url = None
        if self.role == "mbp" and self.mini_url:
            remote_url = normalize_status_url(self.mini_url)
            remote = fetch_remote_status(remote_url)
            remote.setdefault("label", "Mini")
            remote.setdefault("role", "mini")
        elif self.role == "mini" and self.mbp_url:
            remote_url = normalize_status_url(self.mbp_url)
            remote = fetch_remote_status(remote_url)
            remote.setdefault("label", "MBP")
            remote.setdefault("role", "mbp")

        mbp = local if local.get("role") == "mbp" else remote
        mini = local if local.get("role") == "mini" else remote
        dual = bool(self.dual or remote_url)
        return {
            "dual": dual and (mbp is not None and mini is not None),
            "local": local,
            "remote": remote,
            "remote_url": remote_url,
            "mbp": mbp,
            "mini": mini,
        }


def make_handler(state: BoardState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler API)
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = PAGE_HTML.replace("__POLL_MS__", str(state.poll_ms))
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in ("/status.json", "/api/local"):
                payload = json.dumps(state.local_status(), indent=2).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if path == "/api/board":
                payload = json.dumps(state.board(), indent=2).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            self._send(404, b'{"error":"not found"}\n', "application/json; charset=utf-8")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Accept")
            self.end_headers()

    return Handler


def serve(state: BoardState, bind: str, port: int, open_browser: bool) -> int:
    try:
        httpd = ThreadingHTTPServer((bind, port), make_handler(state))
    except OSError as exc:
        sys.stderr.write("cannot bind %s:%s — %s\n" % (bind, port, exc))
        return 1
    display_host = "127.0.0.1" if bind in ("0.0.0.0", "::") else bind
    url = "http://%s:%d/" % (display_host, port)
    sys.stderr.write(
        "embed rate timer  %s  UI %s  status %sstatus.json  bind %s\n"
        % (state.label, url, url, bind)
    )
    if open_browser:

        def _open() -> None:
            time.sleep(0.25)
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001 — best-effort
                sys.stderr.write("webbrowser.open failed: %s\n" % exc)

        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _write_sample_log(path: str, start: float, n: int = 12, step: int = 40, dt: float = 15.0, total: int = 23856) -> None:
    lines = ["# sample MailArchive embed log\n"]
    for i in range(n):
        ts = datetime.datetime.fromtimestamp(start + i * dt).strftime("%Y-%m-%d %H:%M:%S")
        committed = 2500 + i * step
        lines.append("%s committed %d/%d\n" % (ts, committed, total))
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def run_self_test() -> int:
    import tempfile

    failures = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal failures
        if not cond:
            failures += 1
            sys.stderr.write("FAIL  %s\n" % msg)
        else:
            sys.stderr.write("ok    %s\n" % msg)

    parsed = parse_committed_line("2026-09-04 11:07:02 committed 2536/23856")
    check(parsed is not None and parsed[1] == 2536 and parsed[2] == 23856, "parse committed N/M")
    check(parsed is not None and parsed[0] is not None, "parse leading timestamp")
    check(parse_committed_line("nope") is None, "reject non-committed line")
    check(detect_role("mac-mini.local") == "mini", "role mini from hostname")
    check(detect_role("Kools-MacBook-Pro.local") == "mbp", "role mbp from hostname")
    check(format_eta(0) == "0s", "eta zero")
    check(format_eta(90 * 60 + 10) == "1h 30m", "eta hours")
    check(normalize_status_url("http://mac-mini.local:17854") == "http://mac-mini.local:17854/status.json", "url normalize")

    td = tempfile.mkdtemp(prefix="embed-rate-")
    live = os.path.join(td, "embed_full_20260904_110702_numctx8192.log")
    dry = os.path.join(td, "embed_full_20260904_100000_dryrun.log")
    shard = os.path.join(td, "embed_shard1_20260904_120000.log")
    now = time.time()
    start = now - 165
    _write_sample_log(live, start)
    with open(dry, "w", encoding="utf-8") as fh:
        fh.write("2026-09-04 10:00:00 committed 1/2\n")
    os.utime(dry, (now - 50, now - 50))
    with open(shard, "w", encoding="utf-8") as fh:
        fh.write("2026-09-04 12:00:00 committed 10/100\n")
    os.utime(shard, (now - 10, now - 10))
    os.utime(live, (now, now))

    found_mbp = find_log(td, "mbp")
    check(found_mbp == live, "newest embed_full, skip dryrun")
    found_mini = find_log(td, "mini")
    check(found_mini == shard, "mini prefers embed_shard1")

    snap = snapshot_log(live, "mbp", "MBP", "test-host", 180, 90, now=now)
    check(snap["ok"] is True, "snapshot ok")
    check(snap["committed"] == 2500 + 11 * 40, "latest committed")
    check(snap["total"] == 23856, "total")
    check(snap["rate_per_hour"] is not None and snap["rate_per_hour"] > 0, "positive rate")
    # 40 items / 15s = 9600/hr
    check(snap["rate_per_hour"] is not None and 8000 <= snap["rate_per_hour"] <= 12000, "rate ~9600/hr")
    check(snap["eta_human"] != "—", "eta present")
    check(snap["remaining"] == 23856 - (2500 + 11 * 40), "remaining")

    check("tkinter" not in sys.modules, "tkinter not imported")
    src_lines = open(__file__, "r", encoding="utf-8").read().splitlines()
    import_hits = [
        ln
        for ln in src_lines
        if re.match(r"\s*(import tkinter|from tkinter\b)", ln)
    ]
    check(not import_hits, "source has no tkinter import")

    if failures:
        sys.stderr.write("%d check(s) failed\n" % failures)
        return 1
    sys.stderr.write("self-test passed\n")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Browser digital-timer for MailArchive embed backfill rates (no Tk).",
        epilog="""
examples (Apple /usr/bin/python3):

  # Mode A — this Mac only (opens browser)
  /usr/bin/python3 scripts/embed_rate_timer.py --role mbp
  /usr/bin/python3 scripts/embed_rate_timer.py --role mini

  # Mode B — Mini publishes status (allow inbound TCP 17854 / Little Snitch)
  /usr/bin/python3 scripts/embed_rate_timer.py --role mini --serve 17854 --no-open

  # Mode B — MBP dual pane (MBP + Mini) when LAN is open
  /usr/bin/python3 scripts/embed_rate_timer.py --role mbp --dual \\
      --mini-url http://mac-mini.local:17854/status.json
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--role", choices=("auto", "mbp", "mini"), default="auto", help="pane label + default log glob (default: auto from hostname)")
    p.add_argument("--label", default=None, help="override pane label (default: MBP or Mini)")
    p.add_argument("--log", default=None, help="explicit log path (otherwise newest matching glob)")
    p.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR, help="log directory (default: ~/MailArchive/logs)")
    p.add_argument("--window-sec", type=int, default=DEFAULT_WINDOW_SEC, help="rolling rate window, 120–300 (default 180)")
    p.add_argument("--stale-sec", type=int, default=DEFAULT_STALE_SEC, help="mark stale if log mtime older than this")
    p.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS, help="browser poll interval (default 1000)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port (default %d)" % DEFAULT_PORT)
    p.add_argument("--bind", default=None, help="bind address (default 127.0.0.1; 0.0.0.0 with --serve)")
    p.add_argument("--serve", type=int, nargs="?", const=DEFAULT_PORT, metavar="PORT", help="LAN status server: bind 0.0.0.0 and serve HTML + /status.json")
    p.add_argument("--mini-url", default=None, help="Mini status URL, e.g. http://mac-mini.local:17854/status.json")
    p.add_argument("--mbp-url", default=None, help="MBP status URL when this process is the Mini")
    p.add_argument("--dual", action="store_true", help="two-pane board; default Mini URL is http://mac-mini.local:%d/status.json" % DEFAULT_PORT)
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--json", action="store_true", help="print one local status snapshot and exit")
    p.add_argument("--self-test", action="store_true", help="run parser/rate checks and exit")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()

    role = detect_role() if args.role == "auto" else args.role
    label = args.label or role_label(role)
    window = max(120, min(300, int(args.window_sec)))
    poll_ms = max(500, min(2000, int(args.poll_ms)))

    mini_url = args.mini_url
    mbp_url = args.mbp_url
    dual = bool(args.dual or mini_url or mbp_url)
    if args.dual and role == "mbp" and not mini_url:
        mini_url = "http://%s:%d/status.json" % (DEFAULT_MINI_HOST, args.serve or args.port)
    if args.dual and role == "mini" and not mbp_url and not mini_url:
        sys.stderr.write("note: --dual on Mini needs --mbp-url http://MBP-HOST:%d/status.json\n" % (args.serve or args.port))

    state = BoardState(
        logs_dir=os.path.expanduser(args.logs_dir),
        role=role,
        label=label,
        log_path=args.log,
        window_sec=window,
        stale_sec=int(args.stale_sec),
        poll_ms=poll_ms,
        mini_url=mini_url,
        mbp_url=mbp_url,
        dual=dual,
    )

    if args.json:
        print(json.dumps(state.local_status(), indent=2))
        return 0

    port = int(args.serve if args.serve is not None else args.port)
    if args.bind:
        bind = args.bind
    elif args.serve is not None:
        bind = "0.0.0.0"
    else:
        bind = "127.0.0.1"

    return serve(state, bind, port, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
