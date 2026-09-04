# Mailroom semantic search (Mac-local)

OpenAI `text-embedding-3-small` (1536-d) + sqlite-vec over bodies already in
`messages_fts`. FTS exact-id search stays on FTS — this does not replace it.

Copy onto MacBook-Pro.local:

```bash
mkdir -p ~/MailArchive/scripts
cp scripts/*.py scripts/*.sql scripts/README.md ~/MailArchive/scripts/
# from a clone:
# cp /path/to/koolkurkle/scripts/* ~/MailArchive/scripts/
```

Default DB: `~/MailArchive/mailroom.sqlite`. Scripts take `--db`.

`mailroom_tools.py` is not in older clones — it is a thin wrapper that adds
`semantic_search` (list of dicts). There is no `search_mail` in this repo;
FTS remains the Mac Mailroom index.

## Install sqlite-vec on macOS arm64

Apple `/usr/bin/python3` **cannot load SQLite extensions**. Use Homebrew Python.

```bash
brew install python
# Apple Silicon Homebrew python:
#   /opt/homebrew/bin/python3
/opt/homebrew/bin/python3 -m pip install sqlite-vec
# or from the repo:
# /opt/homebrew/bin/python3 -m pip install -r requirements-embed.txt
```

The `sqlite-vec` wheel vendors `vec0` for macosx_arm64. Optional: Homebrew
SQLite CLI (keg-only) to inspect the file — not required for the scripts.

```bash
brew install sqlite
# /opt/homebrew/opt/sqlite/bin/sqlite3 ~/MailArchive/mailroom.sqlite
```

If you prefer a loadable dylib instead of pip:

1. Download the **macos-aarch64** `vec0.dylib` from
   [sqlite-vec releases](https://github.com/asg017/sqlite-vec/releases).
2. `mkdir -p ~/MailArchive/lib && mv vec0.dylib ~/MailArchive/lib/`
3. Pass `--vec-extension ~/MailArchive/lib/vec0.dylib`

There is no official `brew install sqlite-vec` formula as of this writing.
`brew install python` + `pip install sqlite-vec` is the arm64 path.

## Keychain (OpenAI API key)

Preferred service/account (tried first):

| service           | account     |
| ----------------- | ----------- |
| `openai-api-key`  | `koolkurkle` |

Also tried, in order, if the preferred item is missing:
`OpenAI API Key`/`koolkurkle`, `OpenAI`/`api-key`, `openai`/`OPENAI_API_KEY`,
`com.openai.api`/`koolkurkle`. The script fails clearly if none exist.

**macOS Tahoe:** `-w` must be the last argument to `security add-generic-password`.

Interactive (prompts for the password; nothing lands in shell history):

```bash
security add-generic-password -a koolkurkle -s openai-api-key -w
```

If you pass the secret on the command line, it is still last:

```bash
security add-generic-password -a koolkurkle -s openai-api-key -w 'YOUR_KEY'
```

Read (scripts do this; `-w` last; they never print the result):

```bash
security find-generic-password -s openai-api-key -a koolkurkle -w
```

`OPENAI_API_KEY` overrides Keychain **for tests / one-off only**. Do not export
it in a shared shell. The scripts never print or log the key.

## Backfill then search

Dry-run lists candidate counts (skips `lane=auth` and already-embedded same
model+version+hash):

```bash
cd ~/MailArchive/scripts
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dry-run
```

Embed (resume-safe; `--limit` for a slice):

```bash
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite
```

One-liner search after backfill:

```bash
/opt/homebrew/bin/python3 ~/MailArchive/scripts/semantic_search.py --db ~/MailArchive/mailroom.sqlite --k 10 'receipt from apple'
```

Optional after-8pm hook (launchd/cron). Does not run OpenAI until you schedule it:

```bash
# crontab example — 8:15pm local, 200 msgs/night
15 20 * * * /opt/homebrew/bin/python3 ~/MailArchive/scripts/embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200
```

## What gets embedded

- Join `messages_fts.id = messages.id` with a **non-empty FTS `body`**.
- Sources: dump-backed **and** `source='imap-live'` (any source once it has a body).
- **Always skip `lane=auth`** (2FA / auth mail). `--skip-auth` is on by default.
- v1 payload: first **24000 characters** of `subject + "\\n\\n" + body`
  (~6k tokens; model max is 8191). SHA-256 of that string is `text_hash`.
- Idempotent: skip ids that already have an embedding for the same
  `model` + `model_version` and the same `text_hash`. Body edits re-embed.

## Tests (no network, no Keychain)

From the repo root:

```bash
python3 -m pip install -r requirements-embed.txt
python3 -m unittest discover -s tests -v
```
