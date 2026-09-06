# Mini daily RAG (LaunchAgent)

Steady-state Mailroom pipeline on **mac-mini.local** (set your macOS login).
Entirely local: no Grok Bot, no cloud embed, no dual-write over SMB/NFS.

This repo ships the **driver**, **plist**, and **ask_mail stub**. It wires
scripts that already live in `~/MailArchive/scripts` (headers / FTS / 8pm
classify+bills / `embed_backfill.py`). Do not treat this PR as a rewrite of
those tools.

## Source of record

Default driver DB: Mini-local `~/MailArchive/mailroom.sqlite`.

Prefer the Mini as the 24/7 source of record when the copy/merge from the
MBP is done. **One writer.** Do not mount the live SQLite over SMB/NFS and
do not dual-write.

### Promote Mini to SoR (after MBP copy/merge)

1. Pause LaunchAgents / cron on **both** Macs (MBP 8pm and Mini daily).
2. Backup `~/MailArchive` on both machines (`scp` push; not Grok Bot CopyToBox).
3. Copy or merge the MBP DB onto the Mini as
   `~/MailArchive/mailroom.sqlite` (or merge embed shards first, then copy).
4. Confirm only the Mini will write: disable MBP writers that touch this file.
5. Point Mini scripts at `~/MailArchive/mailroom.sqlite` (`MAILROOM_DB` /
   `--db`). Bootstrap `com.mailroom.daily` on the Mini only.
6. Keep the MBP as a laptop replica if you want — **read-only copy**, not a
   second live writer.

## Pipeline

`run_mailroom_daily.sh` → `mailroom_daily.py`:

1. **Headers** — `imap_newmail.py` then `imap_tombstone.py`. Apple
   `/usr/bin/curl` (`CURL_BIN=/usr/bin/curl`). No Python IMAP sockets.
2. **Body / FTS** — first of `imap_fetch_bodies_fts.py`,
   `imap_fetch_bodies.py`. `CURL_BIN` is **unset** so the canonical body
   script can pick Homebrew curl ≥ 8.17. New mail only; skip `lane=auth` /
   auth-shaped / junk inside that script.
3. **Classify + bills** — `classify.py` then `notify_bills.py` (same chain
   as `mailroom_8pm.py`).
4. **Incremental embed** — `embed_backfill.py --skip-auth --quote-strip --lock`
   with Mini `~/MailArchive/.venv/bin/python` and local Ollama
   `qwen3-embedding:8b` → sqlite-vec. MAILROOM §6.1: quote/signature-strip,
   thread graph, header-prefixed document (`instruct_version=v1`,
   `quote_stripped=1`, 1024-d). Resume-safe: missing from `embedding_meta`
   **or** stale `content_hash`. Does **not** restart live rem rows (meta
   present, `content_hash` NULL). Writer lock is per batch, not the rem
   job. Live rem LaunchAgents keep the old text path until EXIT.
5. **ask_mail** is a retrieve stub (not part of the nightly chain).

Stamp: `~/MailArchive/logs/last_daily_rag_ok` is written **only** when the
full chain exits 0. Missing or ≥ ~24h (15-minute slop so 20:00 calendar
is not skipped) → run. Younger stamp → exit 0 with no output (RunAtLoad
catch-up).

## Python

| Step | Interpreter |
|---|---|
| Driver, headers, FTS, classify, bills | `/usr/bin/python3` |
| Embed + ask_mail (sqlite-vec) | `~/MailArchive/.venv/bin/python` |

Apple `/usr/bin/python3` **cannot load sqlite-vec** (no extension API in
that build). PEP 668: do not `pip install` onto the system Python. Create
the venv once:

```zsh
/opt/homebrew/bin/python3 -m venv ~/MailArchive/.venv
~/MailArchive/.venv/bin/python -m pip install sqlite-vec
```

## Keychain

Service **name only**: `mailroom.icloud.app-password`.

The LaunchAgent wrapper calls `/usr/bin/security find-generic-password -s … -w`
and exports `IMAP_APP_PASSWORD` for child IMAP scripts. Nothing in this
repo stores the value. Create the item on the Mini (example — run locally,
do not paste the secret into chat or git):

```zsh
# you type the password at the prompt; it is not in the command line
security add-generic-password -a "$USER" -s mailroom.icloud.app-password -w
```

## Install on Mini (LaunchAgent)

Copy driver files into the silo, then bootstrap for the GUI session:

```zsh
# Mini — substitute __HOME__ with $HOME (launchd does not expand $HOME)
mkdir -p ~/MailArchive/scripts ~/MailArchive/logs ~/Library/LaunchAgents
cp scripts/run_mailroom_daily.sh scripts/mailroom_daily.py scripts/ask_mail.py \
  ~/MailArchive/scripts/
chmod +x ~/MailArchive/scripts/run_mailroom_daily.sh
sed "s|__HOME__|$HOME|g" launchd/com.mailroom.daily.plist \
  > ~/Library/LaunchAgents/com.mailroom.daily.plist

launchctl bootout gui/$(id -u)/com.mailroom.daily 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mailroom.daily.plist
launchctl enable gui/$(id -u)/com.mailroom.daily
# optional one-shot:
# launchctl kickstart -k gui/$(id -u)/com.mailroom.daily
```

Plist:

- `StartCalendarInterval` 20:00 local (matches the 8pm mailroom chain)
- `RunAtLoad` true — catch-up via the stamp
- `KeepAlive` false
- `PATH`, `PYTHONUNBUFFERED=1`
- stdout / stderr under `~/MailArchive/logs/daily_rag.std{out,err}.log`
- checked-in plist is a template (`__HOME__`); install substitutes `$HOME`

Manual:

```zsh
# plan only (needs the Mini scripts on disk)
/usr/bin/python3 ~/MailArchive/scripts/mailroom_daily.py --print-plan
# ignore stamp
/usr/bin/python3 ~/MailArchive/scripts/mailroom_daily.py --force
```

## cron fallback

Prefer LaunchAgent. If you must use cron on the Mini:

```cron
0 20 * * * /bin/zsh $HOME/MailArchive/scripts/run_mailroom_daily.sh
```

## Mini vs MBP

| | Mini (this job) | MBP |
|---|---|---|
| Role | 24/7 SoR when promoted | Laptop; pause writers after cutover |
| Scheduler | `com.mailroom.daily` | Do not also run a live writer on the same DB |
| Embed Python | `~/MailArchive/.venv/bin/python` | Homebrew `/opt/homebrew/bin/python3` on embed PRs |
| Headers curl | Apple `/usr/bin/curl` | Same |

## ask_mail stub

```zsh
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/ask_mail.py 'receipt from apple'
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/ask_mail.py --fts-only --k 5 'invoice'
```

Hybrid FTS + sqlite-vec (if `embed_lib.py` is beside it). LM Studio is
**not** wired (`--llm` exits 2 unless `--allow-llm-stub`).
