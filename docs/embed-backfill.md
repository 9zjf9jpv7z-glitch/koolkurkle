# embed_backfill — one writer per sqlite (HARD DECK)

Preferred practice **before** any `embed_backfill.py` start. Read this
first. Human Terminal cards: [ops-terminal.md](ops-terminal.md). Daily
incremental: [README.mailroom-daily.md](../scripts/README.mailroom-daily.md).
Per-batch lock: [pr0/with_writer_lock_DESIGN.md](pr0/with_writer_lock_DESIGN.md).
Integrity: [sor-health.md](sor-health.md).

## One writer per `.sqlite` (HARD DECK)

Start **one** `embed_backfill` process against a given `.sqlite`.
`--lock` takes the PR-0 writer lock **per batch / heartbeat** (not the
whole rem). It refuses `ACTION_REQUIRED` and a lock held >4h. It does
**not** make two writers on the same file safe. Same-file 2-wide
stays HARD DECK even with `--lock`.

`--lock` still belongs on the Mini daily incremental path. Use it for
that heartbeat / refuse behavior. Do not treat it as a 2-wide permit.

## Shard on separate files, then merge

Preferred split: **separate DBs and/or machines**, then merge.

Example names only:

| Role | File |
|---|---|
| Copy host (Mini copy-only until PR-5) | `mailroom-copy.sqlite` (or `mailroom-daily-copy.sqlite`) |
| SoR host | SoR-named `mailroom.sqlite` |

1. One writer per file (char-band or `id-mod` / `id-rem` shard).
2. Wait until **both** writers **EXIT 0**.
3. Pause the other writer for the **merge window only**.
4. Run `embed_merge_shards.py` (`embed_lib.merge_shards`: missing-only
   embed rows into primary). Resume after merge EXIT 0.

```zsh
# copy host — short band on the copy file
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/embed_backfill.py --db $HOME/MailArchive/mailroom-copy.sqlite --max-chars 3000
```

```zsh
# SoR host — long band on the SoR-named file
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/embed_backfill.py --db $HOME/MailArchive/mailroom.sqlite --min-chars 3000
```

```zsh
# merge host — after both embed writers EXIT 0; other writer paused
$HOME/MailArchive/.venv/bin/python $HOME/MailArchive/scripts/embed_merge_shards.py
```

Same rule for `--id-mod` / `--id-rem`: separate files (and/or hosts),
then `embed_merge_shards.py` after both EXIT 0.

## Same-file char-bands (HARD DECK)

Parallel `--max-chars` / `--min-chars` (or two `id-mod` remainders) on
**one** file are HARD DECK. Preferred same-file pattern is sequential bands:
short band EXIT 0, then the long band on that same file.

A **single** argv closed band (`--min-chars 1500 --max-chars 2000`) is
one writer — that is fine. Parallel bands belong on **separate files**
only (copy vs SoR-named, above).

## Known-good recopy (do not merge-back a bad copy)

If a working copy is malformed (`malformed btree`,
`sqlite3.DatabaseError` on incremental write, or
`PRAGMA integrity_check` not `ok`):

1. Set that working copy **aside**. Do not merge it into SoR or the
   other shard.
2. Recopy from a **known-good** source (SoR-named file or last
   known-good backup).
3. Confirm `PRAGMA integrity_check` prints `ok`.
4. Then start **one** writer.

```zsh
# copy host — integrity before a new embed_backfill
sqlite3 "$HOME/MailArchive/mailroom-copy.sqlite" 'PRAGMA integrity_check;'
```

```zsh
# SoR host — integrity before a new embed_backfill
sqlite3 "$HOME/MailArchive/mailroom.sqlite" 'PRAGMA integrity_check;'
```

Expected: one line `ok`. The health pack hard-fails otherwise:
[sor-health.md](sor-health.md).
