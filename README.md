# koolkurkle

iCloud mail retrieve scripts.

## Mailroom semantic search

Mac-local **Ollama `qwen3-embedding:8b`** embeddings + sqlite-vec over FTS
bodies already in `~/MailArchive/mailroom.sqlite`. No OpenAI. Cloud CI
never calls a network embedder and never downloads the model.

Official Ollama tag (2026):
[qwen3-embedding:8b](https://ollama.com/library/qwen3-embedding:8b).

Full Mac install (Homebrew Python + Ollama), backfill, and the one-liner
search: **[scripts/README.md](scripts/README.md)**.

Copy to the silo:

```bash
mkdir -p ~/MailArchive/scripts
cp scripts/*.py scripts/*.sql scripts/README.md ~/MailArchive/scripts/
```

```bash
brew install python ollama
ollama pull qwen3-embedding:8b
/opt/homebrew/bin/python3 -m pip install sqlite-vec

# dry-run (skip auth + already-embedded; no Ollama call)
/opt/homebrew/bin/python3 ~/MailArchive/scripts/embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dry-run

# backfill when Ollama is serving the model locally
/opt/homebrew/bin/python3 ~/MailArchive/scripts/embed_backfill.py --db ~/MailArchive/mailroom.sqlite --limit 200

# search after backfill (embeds the query on localhost)
/opt/homebrew/bin/python3 ~/MailArchive/scripts/semantic_search.py --db ~/MailArchive/mailroom.sqlite --k 10 'receipt from apple'
```

`mailroom_tools.semantic_search` is the programmatic equivalent. FTS
exact-id lookup is unchanged (`messages_fts`); this is cosine KNN only.

Tests (sqlite-vec + mocked Ollama HTTP + fake vectors, no network):

```bash
python3 -m pip install -r requirements-embed.txt
python3 -m unittest discover -s tests -v
```
