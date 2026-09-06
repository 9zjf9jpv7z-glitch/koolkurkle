# koolkurkle

iCloud mail retrieve scripts.

## Mini daily RAG

LaunchAgent `com.mailroom.daily` on **mac-mini.local** (set your macOS login) runs the
local IMAP → FTS → classify/bills → incremental embed chain. No Grok Bot at
runtime. Default SoR is Mini-local `~/MailArchive/mailroom.sqlite` — one
writer, no SMB/NFS dual-write.

Install, Keychain **name** (`mailroom.imap.app-password`), Mini vs MBP
cutover, and the 24h `last_daily_rag_ok` stamp:
**[scripts/README.mailroom-daily.md](scripts/README.mailroom-daily.md)**.

New/daily embed uses `--quote-strip` (MAILROOM §6.1 header-prefixed cleaned
body). Live rem LaunchAgents keep the old text path until EXIT — do not
restart the 63k backfill or change rem flags.

## Hybrid retrieve (MAILROOM §6.2 / PR-6 + PR-7)

`retrieve(query, k=20, lane=None, after=None, before=None)` in
`scripts/semantic_search.py` fuses FTS5 BM25 + sqlite-vec KNN with RRF,
then scores the fused top-20 with local **Qwen3-Reranker-0.6B**. Query
embed is instruct_version=v1 / 1024-d only (no 63k re-embed). If the
reranker is missing/down, retrieve fail-opens (`rerank=None`, RRF order).
`--no-rerank` forces that stub. Pull + Mini/MBP recipes:
**[docs/rerank.md](docs/rerank.md)**.
ask_mail / HTTP / MCP stay out of scope (PR-8).

Lane + date: FTS **pre-filter** on `messages.lane` / `messages.date_utc`;
vec **post-filter** after KNN. `lane=None` infers money / people / none.
If the lane was inferred and vec is empty after that filter, vec is re-run
without the lane filter (live SoR lanes are sparse; FTS stays filtered).
Explicit `--lane` stays strict. Recency `exp(-0.002 * age_days)` is skipped
when `after`/`before` is set. Vec KNN selects `message_id, distance` only
(live vec0 has no `v.rowid`).

Mac smoke (Mini venv — Apple `/usr/bin/python3` cannot load sqlite-vec):

```zsh
~/MailArchive/.venv/bin/python scripts/semantic_search.py 'SDGE bill'
~/MailArchive/.venv/bin/python scripts/semantic_search.py --json --k 20 'Caddell'
~/MailArchive/.venv/bin/python scripts/semantic_search.py --lane money --after 2024-01-01 'invoice'
~/MailArchive/.venv/bin/python scripts/semantic_search.py --cosine 'SDGE bill'
~/MailArchive/.venv/bin/python scripts/semantic_search.py --no-rerank 'SDGE bill'
~/MailArchive/.venv/bin/python scripts/semantic_search.py 'horse'
# optional, once: populate messages_ids.identifiers (additive; no column rename)
~/MailArchive/.venv/bin/python scripts/messages_ids.py --db ~/MailArchive/mailroom.sqlite --backfill
# once per machine: ollama pull dengcao/Qwen3-Reranker-0.6B && ollama cp dengcao/Qwen3-Reranker-0.6B qwen3-reranker:0.6b
```

## SoR health (integrity + FTS/hybrid smoke)

Read-only check of `$HOME/MailArchive/mailroom.sqlite` (or `$MAILROOM_DB`).
Recipes, Mini copy-DB note, and exit codes:
**[docs/sor-health.md](docs/sor-health.md)**.

```zsh
# MBP — SoR health + hybrid smoke
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py
```

```zsh
# Mini — SoR health + hybrid smoke (copy DB is OK; not a second writer)
~/MailArchive/.venv/bin/python ~/MailArchive/scripts/sor_health_pack.py
```

## macos-slim (Mini only)

SIP-safe Photos/media-analysis slimming on the Mac Mini M4 24GB (Tahoe
~26.3): **[macos-slim/README.md](macos-slim/README.md)**. Default
`mode=off` after install. Not for the MBP.
