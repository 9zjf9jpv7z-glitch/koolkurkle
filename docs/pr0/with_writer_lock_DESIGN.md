# PR-0 writer lock — MAILROOM.md §9.5

Lock file: `~/MailArchive/mailroom.write.lock`

Mechanism: exclusive `flock` (advisory). Holder writes `PID` / `hostname` / `purpose` / ISO timestamp into the lock file.

If the lock is held longer than **4 hours**, refuse. **Do not steal.**

Writers take the lock. `ask_mail` does **not**.

Action-required open ⇒ no lock / no writes.

## Who takes the lock

| Actor | Lock |
|---|---|
| IMAP header/body ingest, classify, bills, embed backfill, schema DDL | Yes — wrap with `scripts/with_writer_lock.py` |
| `ask_mail` retrieve (FTS / sqlite-vec) | No |
| Humans inspecting with `sqlite3` read-only | No |

This PR ships the wrapper only. It is **not** wired into `embed_backfill` or the Mini daily driver (live embeds stay up).

## Stale lock (>4h)

`flock` is released when the holder process exits (kernel drops the fd). A lock held >4h means a **live** writer has kept exclusive flock that long.

Policy:

1. Try `LOCK_EX | LOCK_NB`.
2. If acquired, overwrite metadata and run the command.
3. If not acquired, read metadata. If `acquired_at` age **> 4 hours**, exit non-zero: held too long, **no steal**.
4. If not acquired and age ≤ 4 hours, exit non-zero: held by PID/host/purpose since timestamp.

Never unlink, truncate, or overwrite a lock file that another process still flocks.

## Action-required

If `~/MailArchive/ACTION_REQUIRED` exists, refuse: take no lock, run no command, write nothing. Clear the file (human) before writers resume.

Override path: `--action-required-file` or `MAILROOM_ACTION_REQUIRED`.

## CLI

```text
with_writer_lock.py --purpose X -- cmd...
```

`--purpose` is required (goes into the lock metadata). Everything after `--` is the writer command. The parent holds flock until the child exits.

```text
with_writer_lock.py --purpose embed_backfill -- \
  ~/MailArchive/.venv/bin/python embed_backfill.py --db mailroom.sqlite
```

Testing overrides: `--lock-file`, `--max-age-hours` (default 4).
