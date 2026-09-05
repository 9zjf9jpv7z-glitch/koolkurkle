-- Mailroom semantic embeddings (sqlite-vec).
-- Applied by embed_backfill.py after the sqlite-vec extension is loaded.
-- Do not run this file in a stock `sqlite3` that cannot `.load` vec0.
--
-- Model: local Ollama qwen3-embedding:8b
--   https://ollama.com/library/qwen3-embedding:8b
-- Stored model id: qwen3-embedding-8b
-- Native output is 4096-d. v1 stores Matryoshka-truncated 1024-d
-- (first 1024 dims, L2-renormalized) so each row is 4 KiB instead of
-- 16 KiB. Qwen3-Embedding is trained for flexible output dims (32–4096).
-- Use --dims 4096 for native (drop + recreate message_embeddings first).
--
-- If you previously applied the OpenAI text-embedding-3-small schema
-- (float[1536]), drop those tables before backfill:
--   DROP TABLE IF EXISTS message_embeddings;
--   DROP TABLE IF EXISTS embedding_meta;
--
-- PR-2 incremental (--quote-strip) does NOT apply this file and must
-- never DROP / rebuild / recreate live vec0. Live SoR already has
-- message_embeddings float[1024].
--
-- Key: messages.id (TEXT). Bodies live in messages_fts.body, not messages.body.
--
-- embedding_meta.text_hash is SHA-256 of the exact truncated subject+body
-- string sent to Ollama, so a later FTS body edit can re-embed.

CREATE TABLE IF NOT EXISTS embedding_meta (
  message_id TEXT NOT NULL,
  model TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT 'v1',
  created_at TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  char_count INTEGER NOT NULL DEFAULT 0,
  dims INTEGER NOT NULL DEFAULT 1024,
  PRIMARY KEY (message_id, model, model_version)
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_embeddings USING vec0(
  message_id TEXT PRIMARY KEY,
  embedding float[1024] distance_metric=cosine
);
