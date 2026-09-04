# koolkurkle

iCloud mail retrieve scripts.

## Mailroom semantic search

Mac-local OpenAI embeddings + sqlite-vec over FTS bodies already in
`~/MailArchive/mailroom.sqlite`. Cloud CI never calls OpenAI and never
touches Keychain.

Full Mac install, Keychain (`-w` last on Tahoe), backfill, and the
one-liner search: **[scripts/README.md](scripts/README.md)**.

Copy to the silo:

```bash
mkdir -p ~/MailArchive/scripts
cp scripts/*.py scripts/*.sql scripts/README.md ~/MailArchive/scripts/
```

```bash
# dry-run (skip auth + already-embedded)
/opt/homebrew/bin/python3 ~/MailArchive/scripts/embed_backfill.py --db ~/MailArchive/mailroom.sqlite --dry-run

# search after backfill
/opt/homebrew/bin/python3 ~/MailArchive/scripts/semantic_search.py --db ~/MailArchive/mailroom.sqlite --k 10 'receipt from apple'
```

`mailroom_tools.semantic_search` is the programmatic equivalent. FTS
exact-id lookup is unchanged (`messages_fts`); this is cosine KNN only.

Tests (sqlite-vec + fake vectors, no network):

```bash
python3 -m pip install -r requirements-embed.txt
python3 -m unittest discover -s tests -v
```
