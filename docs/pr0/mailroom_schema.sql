-- PR-0 live .schema — MacBook-Pro.local SoR
-- As-of: 2026-09-05 ~2:52 PM PT
-- Source: ~/MailArchive/mailroom.sqlite
-- Note: PR-1 must skip existing in_reply_to / content_hash.

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  folder TEXT,
  date_utc TEXT,
  from_addr TEXT,
  from_name TEXT,
  to_addrs TEXT,
  cc_addrs TEXT,
  subject TEXT,
  snippet TEXT,
  size_bytes INTEGER,
  lane TEXT,
  urgent INTEGER DEFAULT 0,
  junk INTEGER DEFAULT 0,
  injection_flag INTEGER DEFAULT 0,
  body_path TEXT,
  jsonl_offset INTEGER,
  jsonl_len INTEGER,
  uid TEXT,
  flags TEXT,
  present_on_server INTEGER,
  message_id_header TEXT,
  in_reply_to TEXT,
  content_hash TEXT,
  ingested_at TEXT
);
CREATE INDEX idx_messages_date ON messages(date_utc);
CREATE INDEX idx_messages_lane ON messages(lane);
CREATE INDEX idx_messages_from ON messages(from_addr);
CREATE INDEX idx_messages_urgent ON messages(urgent);
CREATE INDEX idx_messages_msgid ON messages(message_id_header);
CREATE INDEX idx_messages_chash ON messages(content_hash);
CREATE INDEX idx_messages_source ON messages(source);
CREATE INDEX idx_messages_present ON messages(present_on_server);
CREATE INDEX idx_messages_uid ON messages(uid);
CREATE INDEX idx_messages_folder ON messages(folder);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  id UNINDEXED, subject, body, from_addr, tokenize='porter unicode61'
);
CREATE TABLE ingest_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE audit (ts TEXT NOT NULL, actor TEXT NOT NULL, tool TEXT, message_id TEXT, lane TEXT, action TEXT NOT NULL, detail TEXT);
CREATE TABLE bills (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, vendor TEXT, amount_cents INTEGER, currency TEXT DEFAULT 'USD', due_date TEXT, account_hint TEXT, status TEXT DEFAULT 'open');
CREATE INDEX idx_bills_due ON bills(due_date);
CREATE INDEX idx_bills_status ON bills(status);
CREATE TABLE notify_log (ts TEXT NOT NULL, message_id TEXT NOT NULL, channel TEXT NOT NULL, result TEXT, UNIQUE(message_id, channel));
CREATE TABLE drafts (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, created_at TEXT NOT NULL, text TEXT NOT NULL, path TEXT NOT NULL, status TEXT DEFAULT 'pending');
CREATE TABLE embedding_meta (
  message_id TEXT NOT NULL, model TEXT NOT NULL, model_version TEXT NOT NULL DEFAULT 'v1',
  created_at TEXT NOT NULL, text_hash TEXT NOT NULL, char_count INTEGER NOT NULL DEFAULT 0,
  dims INTEGER NOT NULL DEFAULT 1024, PRIMARY KEY (message_id, model, model_version)
);
CREATE VIRTUAL TABLE message_embeddings USING vec0(
  message_id TEXT PRIMARY KEY, embedding float[1024] distance_metric=cosine
);
-- plus vec0 internal tables (sqlite-vec shadow/info rows; not restated)
