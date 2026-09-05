# PR-0 schema dump — MacBook-Pro.local SoR

As-of: 2026-09-05 ~2:52 PM PT

SoR: `~/MailArchive/mailroom.sqlite` (708030464 bytes)

PRAGMA: `journal_mode=wal`; `mmap_size=0`; `user_version=0`; `page_size=4096`

SQLite:

- `/usr/bin/sqlite3` **3.51.0**
- Apple python sqlite **3.51.0**
- Homebrew python sqlite **3.53.4** (use for writers)

Live: `embed_backfill --db mailroom.sqlite --min-chars 3000`; dims histogram `1024|54371`; probe **1024**

Tables: `messages` (`+in_reply_to`, `content_hash` already); `messages_fts` porter unicode61; `ingest_meta`; `audit`; `bills`; `notify_log`; `drafts`; `embedding_meta` dims default 1024; `message_embeddings` vec0 `float[1024]`

Note: PR-1 must skip existing `in_reply_to` / `content_hash`.
