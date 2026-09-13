# Mini daily RAG (LaunchAgent)

Steady-state Mailroom pipeline on **mac-mini.local** (set your macOS login).
Entirely local: no Grok Bot, no cloud embed, no dual-write over SMB/NFS.

This repo ships the **driver**, **plist**, and **ask_mail** CLI. It wires
scripts that already live in `~/MailArchive/scripts` (headers / FTS / 8pm
classify+bills / `embed_backfill.py`). Do not treat this PR as a rewrite of
those tools.

## Preferred practice (copy-only until SoR cutover)

The Mini daily job writes **only** a copy DB. Set `MAILROOM_DB` (or `--db`)
to a path whose basename is `mailroom-copy.sqlite` or
`mailroom-daily-copy.sqlite`. The driver **refuses** to start (non-zero,
`db_mode=refused`, no IMAP/embed) if the variable is unset or the basename
is `mailroom.sqlite` or anything else.

**Why:** `mailroom.sqlite` is the SoR name. Until PR-5 cutover, Mini SoR
may be empty while rem embed still holds `mailroom-copy`. A silent default
to `mailroom.sqlite` would write the empty SoR or race the rem job.
Copy-only keeps one writer on the live rem copy and leaves SoR promotion
to PR-5 (out of scope here). Do not mount the live SQLite over SMB/NFS
and do not dual-write.

### Daily children use the same copy path

The driver **and** every daily child must open that copy. `mailroom_daily.py`
passes `--db` and sets `MAILROOM_DB` on headers (`imap_newmail.py`,
`imap_tombstone.py`), body/FTS, classify, bills, and embed. Children read
`$MAILROOM_DB` (launchd already sets it) **and** accept `--db`. Resolve
through `mailroom_copy_db.bind_copy_db()` (same allowlist) — do not use a
hardcoded SoR path (`t.DB` or `~/MailArchive/mailroom.sqlite`).

**PR-36 / child MAILROOM_DB lesson:** driver copy-only is not enough if a
child ignores `--db` / `$MAILROOM_DB`. `bind_copy_db()` / `parse_db_cli()`
must read `sys.argv[1:]` when `argv` is `None`. Treating `None` like `[]`
drops process `--db` and the child can still open Mini's empty SoR stub.
GitHub children (`scripts/imap_newmail.py`, `imap_tombstone.py`,
`imap_fetch_bodies_fts.py`, `classify.py`, `notify_bills.py`) honor that
bind and refuse `mailroom.sqlite` / unset (fail closed). They do not open
IMAP or Keychain. Mini-local live IMAP/classify/bills should call the same
`bind_copy_db()` before any sqlite write — merge the bind; do not replace
a live Mini body with the GitHub bind-only contract.

**Why:** a child that ignores the copy and opens Mini's empty SoR stub
fails with `no such table: messages` even though the copy has `messages`.
`embed_backfill.py` already took `--db`; the other daily children must
honor the same path. Do not invent a second daily driver.

If rem embed still holds `mailroom-copy.sqlite`, point the daily job at
`__HOME__/MailArchive/mailroom-daily-copy.sqlite` instead.

### SoR cutover is PR-5 (out of scope)

Do **not** promote Mini `mailroom.sqlite` or change the allowlist in this
driver. After PR-5, the same label can point at SoR. Until then, refuse
is hard-fail (`db_mode=refused`), not fail-open.

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
5. **ask_mail** is on-demand (CLI / HTTP / MCP) — not part of the nightly
   chain. This job must **not** start LM Studio or load 35B-class generate.
   Generate stays a separate on-demand path. Rerank is CrossEncoder
   (fail-open if the optional extra is missing).

Watermarks (atomic temp+replace) under `~/MailArchive/logs/`:

- `last_imap_ok` — headers IMAP succeeded
- `last_bodies_ok` — body/FTS succeeded
- `last_embed_ok` — incremental embed succeeded
- `last_daily_rag_ok` — written **only** when imap + bodies + embed
  succeeded (classify/bills may warn and still allow this stamp)

Catch-up: `last_daily_rag_ok` missing or ≥ ~24h (15-minute slop so 20:05
calendar is not skipped) → run, **resume first failed phase**, do not redo
successful watermarks from this cycle. Younger daily stamp → exit 0
(RunAtLoad catch-up). Exclusive flock on `mailroom.daily.lock` so
`StartCalendarInterval` + `RunAtLoad` cannot double-run.

Embed health-check: `GET http://127.0.0.1:11434/api/tags` (`OLLAMA_HOST`)
before the embed step. Local Ollama only — not a generate runtime.

## Python

| Step | Interpreter |
|---|---|
| Driver, headers, FTS, classify, bills | `/usr/bin/python3` |
| Embed + ask_mail (sqlite-vec) | `~/MailArchive/.venv/bin/python` |

Apple `/usr/bin/python3` **cannot load sqlite-vec** (no extension API in
that build). PEP 668: do not `pip install` onto the system Python. Create
the venv once:

```zsh
# Mini — create MailArchive venv
/opt/homebrew/bin/python3 -m venv ~/MailArchive/.venv
```

```zsh
# Mini — install sqlite-vec in the venv
~/MailArchive/.venv/bin/python -m pip install sqlite-vec
```

## Keychain

Service **name only** (default): `mailroom.imap.app-password`.

Override the item with `MAILROOM_KEYCHAIN_ITEM` (the LaunchAgent plist sets
this to the default). The wrapper calls
`/usr/bin/security find-generic-password -s … -w` and exports
`IMAP_APP_PASSWORD` for child IMAP scripts. Nothing in this repo stores
the value. Never echo or log the secret.

Keychain must work from **launchd** (`launchctl start com.mailroom.daily`
or the 20:05 calendar). A password that unlocks only in an interactive
Terminal session is not enough — the GUI session Keychain path is
different. Prove the item from `launchctl start`, then read
`~/MailArchive/logs/daily_rag.stderr.log` (length / IMAP success only;
never paste the secret).

One-time **read fallback**: if the default name is missing or empty, the
wrapper tries legacy `mailroom.icloud.app-password` once and warns on
stderr. It does not fail solely because only the old item exists. A
`MAILROOM_KEYCHAIN_ITEM` set to any other name is used as-is (no
legacy fallback). Prefer the new name; keep the legacy item until IMAP
smoke PASSes on `mailroom.imap.app-password`.

Human Terminal cards (one machine, one command per fence):
**[docs/ops-terminal.md](../docs/ops-terminal.md)**.

Create the item locally. Keep `-w` last so the secret is typed only at
the interactive prompt. Do not paste the secret into chat or git.

```zsh
# Mini — create IMAP Keychain item (type the secret at the prompt)
security add-generic-password -a "$USER" -s mailroom.imap.app-password -w
```

```zsh
# Mini — Keychain length check (no secret on stdout)
security find-generic-password -s mailroom.imap.app-password -w | wc -c
```

Apple app-specific passwords are typically ~16–19 characters.
`security -w` may add a trailing newline in `wc -c`. An ~8-character
secret will not authenticate to IMAP (Login denied); regenerate at
appleid.apple.com.

### MBP / Mini migrate recipe

Prefer `mailroom.imap.app-password`. Keep
`mailroom.icloud.app-password` until IMAP smoke PASSes on the new name.
Each `security` line is its own fence (one paste).

```zsh
# MBP — create IMAP Keychain item (type the secret at the prompt)
security add-generic-password -a "$USER" -s mailroom.imap.app-password -w
```

```zsh
# MBP — Keychain length check (no secret on stdout)
security find-generic-password -s mailroom.imap.app-password -w | wc -c
```

```zsh
# MBP — delete legacy Keychain item (only after IMAP smoke PASSes)
# security delete-generic-password -s mailroom.icloud.app-password
```

```zsh
# Mini — create IMAP Keychain item (type the secret at the prompt)
security add-generic-password -a "$USER" -s mailroom.imap.app-password -w
```

```zsh
# Mini — Keychain length check (no secret on stdout)
security find-generic-password -s mailroom.imap.app-password -w | wc -c
```

```zsh
# Mini — delete legacy Keychain item (only after IMAP smoke PASSes)
# security delete-generic-password -s mailroom.icloud.app-password
```

## Install on Mini (LaunchAgent)

Substitute `__HOME__`, copy the **same** driver (`run_mailroom_daily.sh` +
`mailroom_daily.py` + `com.mailroom.daily`). Do **not** add a second
label. Do **not** `launchctl bootout` this label (or rem embed agents)
while rem embed is live — if rem still holds `mailroom-copy.sqlite`, set
`MAILROOM_DB` to `mailroom-daily-copy.sqlite`, then bootstrap the same
label.

```zsh
# Mini — substitute __HOME__ with $HOME (launchd does not expand $HOME)
mkdir -p ~/MailArchive/scripts ~/MailArchive/logs ~/Library/LaunchAgents
```

```zsh
# Mini — copy daily driver + copy-db helper (bind_copy_db)
cp scripts/run_mailroom_daily.sh scripts/mailroom_daily.py \
  scripts/mailroom_copy_db.py \
  ~/MailArchive/scripts/
```

```zsh
# Mini — merge bind_copy_db into live IMAP/classify/bills (do not
# overwrite a live Mini body with the GitHub bind-only contract)
# from mailroom_copy_db import bind_copy_db
# db = bind_copy_db()  # argv=None → sys.argv[1:]; honors --db
```

```zsh
# Mini — make the wrapper executable
chmod +x ~/MailArchive/scripts/run_mailroom_daily.sh
```

```zsh
# Mini — install LaunchAgent from the __HOME__ template
sed "s|__HOME__|$HOME|g" launchd/com.mailroom.daily.plist \
  > ~/Library/LaunchAgents/com.mailroom.daily.plist
```

```zsh
# Mini — if rem embed still holds mailroom-copy, retarget daily-copy
# (edit the installed plist MAILROOM_DB to
#  __HOME__/MailArchive/mailroom-daily-copy.sqlite after sed)
# Do not bootout while rem embed is live.
```

```zsh
# Mini — bootstrap the same label (com.mailroom.daily)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mailroom.daily.plist
```

```zsh
# Mini — enable the daily LaunchAgent
launchctl enable gui/$(id -u)/com.mailroom.daily
```

```zsh
# Mini — prove Keychain from launchd (not Terminal-only)
launchctl start com.mailroom.daily
```

```zsh
# Mini — optional one-shot kickstart
# launchctl kickstart -k gui/$(id -u)/com.mailroom.daily
```

Plist:

- Label `com.mailroom.daily` (single existing driver)
- `MAILROOM_DB=__HOME__/MailArchive/mailroom-copy.sqlite` (or
  `mailroom-daily-copy.sqlite` when rem embed still holds the copy)
- `MAILROOM_KEYCHAIN_ITEM=mailroom.imap.app-password`
- `OLLAMA_HOST=http://127.0.0.1:11434`
- `StartCalendarInterval` 20:05 local (precursor Minute 0; 8pm bills
  digest stays a separate agent)
- `Nice` 5
- `RunAtLoad` true — catch-up via the stamp + flock
- `KeepAlive` false
- `PATH` Homebrew + system, `PYTHONUNBUFFERED=1`
- stdout / stderr under `__HOME__/MailArchive/logs/daily_rag.std{out,err}.log`
- checked-in plist is a template (`__HOME__`); install substitutes `$HOME`

Manual:

```zsh
# Mini — plan only (needs the Mini scripts on disk)
/usr/bin/python3 ~/MailArchive/scripts/mailroom_daily.py --print-plan
```

```zsh
# Mini — ignore stamp
/usr/bin/python3 ~/MailArchive/scripts/mailroom_daily.py --force
```

## cron fallback

Prefer LaunchAgent. If you must use cron on the Mini:

```cron
5 20 * * * MAILROOM_DB=$HOME/MailArchive/mailroom-copy.sqlite /bin/zsh $HOME/MailArchive/scripts/run_mailroom_daily.sh
```

## Mini vs MBP

| | Mini (this job) | MBP |
|---|---|---|
| Role | Copy-only daily until PR-5 | Laptop; rem embed may still hold the copy |
| Scheduler | `com.mailroom.daily` | Do not also run a live writer on the same DB |
| Embed Python | `~/MailArchive/.venv/bin/python` | Homebrew `/opt/homebrew/bin/python3` on embed PRs |
| Headers curl | Apple `/usr/bin/curl` | Same |

## ask_mail (PR-8)

On-demand retrieve + optional LM Studio generate. Not in the nightly
chain. Recipes, probe, and DoD: **[docs/ask_mail.md](../docs/ask_mail.md)**.

```zsh
# Mini — ask_mail (hits-only unless MAILROOM_GENERATE_MODEL is set)
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/ask_mail.py --json 'SDGE bill'
```

```zsh
# Mini — ask_mail FTS-only
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/ask_mail.py --fts-only --k 5 --json 'invoice'
```

Generate runtime on Mini is **LM Studio** (`/v1/chat/completions`), not
unnamed Ollama 9B/27B. If LM Studio is down: labeled `fail_open` /
`hits_only`. Rerank default is CrossEncoder; missing extra fail-opens.
