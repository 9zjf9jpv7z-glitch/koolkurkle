# Mailroom semantic search (Mac-local, offline)

Local **Ollama `qwen3-embedding:8b`** + sqlite-vec over bodies already in
`messages_fts`. No OpenAI account. After `ollama pull`, backfill and search
stay on the machine.

FTS exact-id search stays on FTS — this does not replace it.

Confirmed against the official Ollama library (2026):
[qwen3-embedding:8b](https://ollama.com/library/qwen3-embedding:8b).
`qwen3-embedding` / `qwen3-embedding:latest` is the same 8B Q4_K_M (~4.7 GB).
Stored model id in `embedding_meta.model` is `qwen3-embedding-8b`.

Copy onto MacBook-Pro.local (M1 Max 32 GB):

```bash
mkdir -p ~/MailArchive/scripts
cp scripts/*.py scripts/*.sql scripts/README.md ~/MailArchive/scripts/
# from a clone:
# cp /path/to/koolkurkle/scripts/* ~/MailArchive/scripts/
```

Default DB: `~/MailArchive/mailroom.sqlite`. Scripts take `--db`.

`mailroom_tools.py` is a thin wrapper that adds `semantic_search` (list of
dicts). There is no `search_mail` in this repo; FTS remains the Mac Mailroom
index.

## Install (once, then offline)

Apple `/usr/bin/python3` **cannot load SQLite extensions**. Use Homebrew
Python at `/opt/homebrew/bin/python3`.

```bash
brew install python ollama
# Apple Silicon Homebrew python:
#   /opt/homebrew/bin/python3

# Start Ollama (GUI app or service). Then pull the official 8B embedder:
ollama pull qwen3-embedding:8b

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

Ollama listens on `http://127.0.0.1:11434` by default (`--ollama-url`).
The scripts POST `/api/embed` (batch), and fall back to OpenAI-compatible
`/v1/embeddings` on the same localhost. Nothing leaves the Mac.

### Fallback (not the default)

If Ollama cannot load the 8B embedder on this machine, MLX or
`sentence-transformers` can produce the same Qwen3-Embedding-8B vectors,
but these scripts do **not** call them. Stay on Ollama unless that path is
blocked.

## Backfill then search

Dry-run lists candidate counts (skips `lane=auth` and already-embedded same
model+version+hash). **Does not call Ollama or any cloud API:**

```bash
cd ~/MailArchive/scripts
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dry-run
```

Embed (resume-safe; `--limit` for a slice). Requires local Ollama with
`qwen3-embedding:8b`:

```bash
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite
```

One-liner search after backfill (embeds the query locally):

```bash
/opt/homebrew/bin/python3 ~/MailArchive/scripts/semantic_search.py --db ~/MailArchive/mailroom.sqlite --k 10 'receipt from apple'
```

Optional after-8pm hook (launchd/cron). Does not call Ollama until you
schedule it, and then only localhost:

```bash
# crontab example — 8:15pm local, 200 msgs/night
15 20 * * * /opt/homebrew/bin/python3 ~/MailArchive/scripts/embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200
```

## What gets embedded

- Join `messages_fts.id = messages.id` with a **non-empty FTS `body`**.
- Sources: dump-backed **and** `source='imap-live'` (any source once it has a body).
- **Always skip `lane=auth`** (2FA / auth mail). `--skip-auth` is on by default.
- v1 document payload: first **16000 characters** of `subject + "\n\n" + body`
  (~4k tokens at 4 chars/token; model context is 32k). SHA-256 of that
  string is `text_hash`. Queries add a Qwen3 instruct prefix (documents do
  not).
- Idempotent: skip ids that already have an embedding for the same
  `model` + `model_version` and the same `text_hash`. Body edits re-embed.

## Dimensions (1024 vs 4096)

`qwen3-embedding:8b` native output is **4096-d**. v1 stores a **1024-d**
Matryoshka prefix (first 1024 floats, L2-renormalized). That is ~4 KiB per
message instead of 16 KiB, and the model is trained for user-defined dims
in 32–4096. Cosine KNN quality for mail search stays high at 1024.

Native 4096:

```bash
# drop the 1024-d table first if it already exists
# /opt/homebrew/opt/sqlite/bin/sqlite3 ~/MailArchive/mailroom.sqlite \
#   "DROP TABLE IF EXISTS message_embeddings; DROP TABLE IF EXISTS embedding_meta;"
/opt/homebrew/bin/python3 embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dims 4096
```

`--dims` must match between backfill and search. The vec0 table is created
at the first `apply_schema` with that length.

If you previously applied the OpenAI `text-embedding-3-small` schema
(`float[1536]` from PR #4), drop those tables before this backfill:

```bash
/opt/homebrew/opt/sqlite/bin/sqlite3 ~/MailArchive/mailroom.sqlite \
  "DROP TABLE IF EXISTS message_embeddings; DROP TABLE IF EXISTS embedding_meta;"
```

## Tests (no network, no model download)

From the repo root:

```bash
python3 -m pip install -r requirements-embed.txt
python3 -m unittest discover -s tests -v
```

CI mocks Ollama HTTP and uses fake 1024-d vectors. It never pulls
`qwen3-embedding:8b` and never calls a cloud API.
