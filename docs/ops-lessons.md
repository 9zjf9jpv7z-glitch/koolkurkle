# Ops lessons

Dated **2026-09-05**. Mac-local Grok Bot / MailArchive runbook. No secrets.

## Large DB Mac↔Mac

- Grok Bot **CopyToBox / CopyFromBox** flaps and caps at **100 MB**. Do not use for large `mailroom.sqlite` (or similar) copies.
- Prefer **`/usr/bin/curl` LAN HTTP** or **`scp` push**.
- Little Snitch inbound often blocks HTTP serve (**connection refused**). Outbound `scp` still works — use that when serve fails.

## Chat sync ≠ local-exec

- Lid closed / sleep: **ListMachines can show connected** while **Shell is unreachable**.
- Wake the Mac (or confirm a live Shell) before treating “connected” as executable.

## macOS TCC

- “Grok Bot would like to access data from other apps” is an **OS dialog outside chat**.
- Warn the user **before** local-exec that touches protected data (Mail, files, other-app data). They must **Allow**.
- Do not assume Allow already happened.

## MBP Python sockets / IMAP

- MBP Python sockets are flaky for IMAP.
- Use **`/usr/bin/curl` imaps** (Apple curl) for **headers-only**.
- Body fetch still needs Homebrew curl ≥ 8.17. Existing notes: `README.imap-curl.md` on the headers-IMAP branch (`cursor/headers-imap-apple-curl-5ec5`).

## Do not Reset/Update Grok Bot’s Computer mid transfer

- **Reset / Update Grok Bot’s Computer** resets the **cloud box**, not the Mac.
- Mid Mac file transfer this can **wipe staged parts**. Finish the copy first.

## Dual-Mac embed

- **Pause before split.**
- **Backup MailArchive on both** Macs.
- **No dual source-of-record writers** (do not dual-write one live `mailroom.sqlite`).
- **Merge only after both EXIT 0.**
