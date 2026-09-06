# SoR health pack

Read-only integrity + FTS / hybrid smoke for the live Mac mailroom SoR.
Does not write the DB, does not kill embed jobs, and does not print message
bodies, Keychain values, or app passwords.

Human Terminal cards (one machine per card, one command per fence):
[ops-terminal.md](ops-terminal.md).

## Path

`$MAILROOM_DB` or `$HOME/MailArchive/mailroom.sqlite` (`Path.home()` /
expanduser — no machine home hardcodes).

**Mini** may use a **copy** DB (for example a file copied from the MBP, or a
local `mailroom-copy`). That is a replica, not a second live writer. Do not
dual-write over SMB/NFS.

## Recipes

```zsh
# MBP — SoR health + hybrid smoke
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py
```

```zsh
# Mini — SoR health + hybrid smoke (copy DB is OK; not a second writer)
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py
```

```zsh
# MBP — explicit SoR path
MAILROOM_DB=$HOME/MailArchive/mailroom.sqlite \
  ~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py
```

Apple `/usr/bin/python3` cannot load sqlite-vec. Use the MailArchive venv
(same interpreter as hybrid retrieve).

## What it checks

- `PRAGMA integrity_check` — **hard fail** if not `ok`
- Counts: `messages`, `message_embeddings` / vec rows, `embedding_meta`,
  coverage gap (messages without embeddings when the schema allows)
- FTS presence + smoke (`bill`, `Caddell`) — hit counts and top **subjects**
  only
- Hybrid smoke via existing `semantic_search.retrieve()` (`SDGE bill` +
  `Caddell`). When embeddings exist, reports whether some hits have a real
  `vec_rank` (not all missing/1000). If Ollama/embed is down, **fail-open**
  and say so
- Backup directory existence only: `$HOME/MailArchive/backups`
- Embed backfill / rem LaunchAgent patterns — PIDs and labels if found.
  Never `kill` / `bootout`

Coverage gaps and missing vec are **warnings** (exit 0). Missing DB is exit 2.

## Copy onto the Mac

The repo script lives at `scripts/sor_health_pack.py`. On the Mac it is
invoked from `~/MailArchive/scripts/` (same layout as `semantic_search.py`).
