-- Mailroom semantic embeddings (sqlite-vec).
-- Applied by embed_backfill.py after the sqlite-vec extension is loaded.
-- Do not run this file in a stock `sqlite3` that cannot `.load` vec0.
--
-- Model: OpenAI text-embedding-3-small, 1536 dimensions (v1).
-- Key: messages.id (TEXT). Bodies live in messages_fts.body, not messages.body.
--
-- embedding_meta.text_hash is SHA-256 of the exact truncated subject+body
-- string sent to the API, so a later FTS body edit can re-embed.

CREATE TABLE IF NOT EXISTS embedding_meta (
  message_id TEXT NOT NULL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT 'v1',
  created_at TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  char_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (message_id, model, model_version)
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_embeddings USING vec0(
  message_id TEXT PRIMARY KEY,
  embedding float[1536] distance_metric=cosine
);
