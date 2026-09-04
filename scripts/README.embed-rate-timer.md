# Embed rate timer

Browser digital-timer / scoreboard for MailArchive Ollama embed backfill rates on two Macs (MBP + Mini).

**No tkinter.** System Tk on `mac-mini.local` aborted with `macOS 26 (2603) or later required, have instead 16 (1603)` then `zsh: abort` — even though the machine is macOS 26.3. This script uses only the Python stdlib (`http.server` + HTML/JS).

Prefer Apple `/usr/bin/python3` or Homebrew `python3`.

## What it shows

For each pane (MBP / Mini):

- huge rolling rate **`/ hr`** (default 180s window, 2–5 min via `--window-sec`)
- latest `committed N/M` from the embed log
- remaining + ETA
- live poll every ~1s (0.5–2s via `--poll-ms`)

Log discovery (newest mtime; skip `*dryrun*`):

| Role | First glob | Fallback |
|------|------------|----------|
| MBP  | `~/MailArchive/logs/embed_full_*.log` | `embed*.log` |
| Mini | `~/MailArchive/logs/embed_shard1_*.log` | `embed*.log` |

Lines parsed: `committed 2536/23856` (optional leading `YYYY-MM-DD HH:MM:SS`). Timestamped lines give an instant rate; otherwise the process builds a rolling window from live polls, or falls back to an overall rate from the filename stamp (`embed_full_20260904_110702_…`).

## Mode A — this Mac only

Opens a local browser UI (`http://127.0.0.1:17854/`) for this machine’s log.

```bash
# MBP (embed_full_*.log)
/usr/bin/python3 scripts/embed_rate_timer.py --role mbp

# Mini (embed_shard1_*.log)
/usr/bin/python3 scripts/embed_rate_timer.py --role mini
```

After CoS copies the script onto a Mac:

```bash
/usr/bin/python3 ~/MailArchive/scripts/embed_rate_timer.py --role mbp
/usr/bin/python3 ~/MailArchive/scripts/embed_rate_timer.py --role mini
```

`--role` can be omitted (`auto` uses the hostname; `mini` in the name → Mini, else MBP). Add `--no-open` to skip `webbrowser.open`.

## Mode B — dual pane (MBP + Mini)

This process always serves HTML **and** JSON:

- UI: `http://HOST:PORT/`
- local status: `http://HOST:PORT/status.json` (also `/api/local`)
- combined board: `http://HOST:PORT/api/board`

LAN between the Macs is the hard part. An earlier Mini `--serve` on `0.0.0.0:17854` was **connection refused** from the MBP (macOS firewall / Little Snitch). SSH key auth is also unreliable (password prompts).

**Option 1 — LAN works** (allow inbound TCP **17854** on Mini: System Settings → Firewall, and Little Snitch):

```bash
# Mini — publish status on the LAN (still serves a UI if you want it)
/usr/bin/python3 scripts/embed_rate_timer.py --role mini --serve 17854 --no-open

# MBP — two panes; server-side fetch of Mini
/usr/bin/python3 scripts/embed_rate_timer.py --role mbp --dual \
    --mini-url http://mac-mini.local:17854/status.json
```

`--dual` on MBP defaults Mini to `http://mac-mini.local:17854/status.json` if `--mini-url` is omitted.

On Mini, pointing back at the MBP:

```bash
/usr/bin/python3 scripts/embed_rate_timer.py --role mini --dual \
    --mbp-url http://MBP-HOST.local:17854/status.json
```

**Option 2 — firewall still blocks** (the usual case): run Mode A on each Mac and open **two browser tabs** (one per machine). Do not rely on SSH.

`--serve PORT` binds `0.0.0.0` so the other Mac can GET `/status.json`. Default Mode A binds `127.0.0.1` only.

## Useful flags

| Flag | Meaning |
|------|---------|
| `--log PATH` | explicit log file |
| `--logs-dir DIR` | default `~/MailArchive/logs` |
| `--window-sec N` | rolling window, clamped 120–300 |
| `--port N` | listen port (default 17854) |
| `--bind ADDR` | override bind address |
| `--json` | one snapshot on stdout, no server |
| `--self-test` | parser/rate checks |
| `--no-open` | do not open a browser |

## Check

```bash
/usr/bin/python3 scripts/embed_rate_timer.py --self-test
/usr/bin/python3 scripts/embed_rate_timer.py --json --log /path/to/embed_full_….log
```
