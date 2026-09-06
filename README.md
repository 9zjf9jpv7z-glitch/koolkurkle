# koolkurkle

iCloud mail retrieve scripts.

## Mini daily RAG

LaunchAgent `com.mailroom.daily` on **mac-mini.local** (login Buck) runs the
local IMAP → FTS → classify/bills → incremental embed chain. No Grok Bot at
runtime. Default SoR is Mini-local `~/MailArchive/mailroom.sqlite` — one
writer, no SMB/NFS dual-write.

Install, Keychain **name** (`mailroom.icloud.app-password`), Mini vs MBP
cutover, and the 24h `last_daily_rag_ok` stamp:
**[scripts/README.mailroom-daily.md](scripts/README.mailroom-daily.md)**.

New/daily embed uses `--quote-strip` (MAILROOM §6.1 header-prefixed cleaned
body). Live rem LaunchAgents keep the old text path until EXIT — do not
restart the 63k backfill or change rem flags.

## macos-slim (Mini only)

SIP-safe Photos/media-analysis slimming on the Mac Mini M4 24GB (Tahoe
~26.3): **[macos-slim/README.md](macos-slim/README.md)**. Default
`mode=off` after install. Not for the MBP.
