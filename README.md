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
