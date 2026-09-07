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
Human Terminal cards, Keychain create, privacy generics, PR description
edit, and Little Snitch:
**[docs/ops-terminal.md](docs/ops-terminal.md)**.

New/daily embed uses `--quote-strip` (MAILROOM §6.1 header-prefixed cleaned
body). Live rem LaunchAgents keep the old text path until EXIT — do not
restart the 63k backfill or change rem flags.

## Hybrid retrieve (MAILROOM §6.2 / PR-6 + PR-7) + ask_mail (PR-8)

`retrieve(query, k=20, lane=None, after=None, before=None)` in
`scripts/semantic_search.py` fuses FTS5 BM25 + sqlite-vec KNN with RRF.
Rerank default is in-process **CrossEncoder**
(`Qwen/Qwen3-Reranker-0.6B`, optional `requirements-rerank.txt`).
Live floats set `Hit.rerank` and `rerank_mode=crossencoder`. Missing
torch/weights or predict failure **fail-opens** (`rerank=None`, RRF,
`rerank_mode=fail_open`). `--no-rerank` forces `rerank_mode=none`.
Ollama generate/chat **cannot** score Qwen3-Reranker. Practice + traps:
**[docs/rerank.md](docs/rerank.md)**,
**[docs/model-runtime-gates.md](docs/model-runtime-gates.md)**.

`scripts/ask_mail.py` is the PR-8 CLI + HTTP `127.0.0.1:8743` (`/ask`;
8744 if bound) + MCP (`ask_mail`, `hybrid_search`, `get_thread`,
non-sending `draft_reply`). Preferred generate **process** is
`mlx_lm.server` on `http://127.0.0.1:1234/v1/chat/completions` when
`$MAILROOM_GENERATE_MODEL` is set; soft-fail to labeled `fail-open-only`
hits-only if down. Ollama is embed-only (never generate). Client path
strings `llmster-headless` / `fail-open-only` stay in code — they are
**not** the process name; withhold the product-name claim
`llmster-headless`. One-command MBP install (copy scripts, stage
LaunchAgent, bootstrap, kickstart, `GET /v1/models`):
**[scripts/install-mlx-generate.sh](scripts/install-mlx-generate.sh)**
— generate-down is `./scripts/install-mlx-generate.sh down` (bootout,
not kill; KeepAlive). Smoke is **retrieve+rerank, then generate** — do
not co-pin Ollama embed 8b, CrossEncoder, and 35B-class generate;
unload embed/rerank between phases. Recipes + DoD:
**[docs/ask_mail.md](docs/ask_mail.md)**,
**[docs/generate-mlx.md](docs/generate-mlx.md)**.
`rerank_mode` is `crossencoder` when live floats land, else labeled
fail-open / none / off (RRF citations; scores not claimed). Do not
co-pin embed + 35B + rerank.

Lane + date: FTS **pre-filter** on `messages.lane` / `messages.date_utc`;
vec **post-filter** after KNN. `lane=None` infers money / people / none.
If the lane was inferred and vec is empty after that filter, vec is re-run
without the lane filter (live SoR lanes are sparse; FTS stays filtered).
Explicit `--lane` stays strict. Recency `exp(-0.002 * age_days)` is skipped
when `after`/`before` is set. Vec KNN selects `message_id, distance` only
(live vec0 has no `v.rowid`).

Mac smoke (Mini venv — Apple `/usr/bin/python3` cannot load sqlite-vec):

```zsh
# Mini — hybrid retrieve
~/MailArchive/.venv/bin/python scripts/semantic_search.py 'SDGE bill'
```

```zsh
# Mini — hybrid retrieve JSON
~/MailArchive/.venv/bin/python scripts/semantic_search.py --json --k 20 'Caddell'
```

```zsh
# Mini — hybrid retrieve lane + after
~/MailArchive/.venv/bin/python scripts/semantic_search.py --lane money --after 2024-01-01 'invoice'
```

```zsh
# Mini — hybrid retrieve cosine
~/MailArchive/.venv/bin/python scripts/semantic_search.py --cosine 'SDGE bill'
```

```zsh
# Mini — hybrid retrieve without rerank
~/MailArchive/.venv/bin/python scripts/semantic_search.py --no-rerank 'SDGE bill'
```

```zsh
# Mini — hybrid retrieve (horse)
~/MailArchive/.venv/bin/python scripts/semantic_search.py 'horse'
```

```zsh
# Mini — optional, once: populate messages_ids.identifiers (additive; no column rename)
~/MailArchive/.venv/bin/python scripts/messages_ids.py --db ~/MailArchive/mailroom.sqlite --backfill
```

```zsh
# Mini — ask_mail (generate_mode/rerank_mode always labeled)
~/MailArchive/.venv/bin/python scripts/ask_mail.py --json 'SDGE bill'
```

```zsh
# Mini — CrossEncoder CRM smoke (fail-open-only without weights)
~/MailArchive/.venv/bin/python scripts/rerank_smoke.py
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
