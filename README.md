# koolkurkle

iCloud mail retrieve scripts for kirkbacon@me.com.

## What ran in the owner's zsh vs elsewhere

| Check | Where | Result |
| --- | --- | --- |
| Apple `/usr/bin/curl` 8.7.1 LIST (33 folders) | Owner **zsh** | Succeeded. App password worked. |
| Apple curl **custom** `UID FETCH n (BODY.PEEK[])` | Owner **zsh** | Failed: `{size}` then **0** RFC822 bytes. |
| Apple curl URL with `;PEEK=1` | Owner **zsh** | curl (3) illegal URL. |
| `/usr/bin/python3` imaplib `IMAP4_SSL` | Owner **zsh** | EBADF in `sock.connect`, before LOGIN. |
| `/usr/bin/openssl s_client` via `retrieve_mail_openssl.py` | Owner **zsh** | `connect: Bad file descriptor` / errno 9, before LOGIN. |
| `/usr/bin/python3` `create_connection` to IMAP | **Another agent process**, not this zsh | Reported success. **Not a zsh result.** |
| Homebrew / `~/.venv` EBADF | That other agent process | Failed. Do not use those Pythons. |

A Linux VM is not proof. An agent-process connect is not their Terminal.

In their zsh, generic POSIX `connect()` (Python, LibreSSL openssl) has failed with EBADF. **Apple curl** is the only IMAP TCP that has worked there, and only for LIST/SEARCH-style commands.

No public repo documents this zsh combo (Python + openssl errno 9, Apple curl LIST works). The empty FETCH bodies **do** match documented curl IMAP behavior:

- [curl#18847](https://github.com/curl/curl/issues/18847) — custom `-X UID FETCH` is handled as LIST/SEARCH and historically does not read `{size}` literals (prints the FETCH line and stops). The fix landed in 2025; their `/usr/bin/curl` is **8.7.1 (2024-03-27)**, so that fix is not in this binary.
- [curl#10076](https://github.com/curl/curl/discussions/10076) — same empty file with `-X FETCH … BODY.PEEK[]`; the URL curl builds itself (`imaps://host/inbox;mailindex=1`, mailbox `;UID=`) downloads the body. PEEK is not in that URL form (`BODY[]`, may set `\Seen`).
- [curl#8659](https://github.com/curl/curl/pull/8659) / [curl#11671](https://github.com/curl/curl/pull/11671) — `-X` UID FETCH vs URL `;UID=`.

iCloud note (not run in their zsh): [mail-imap-mcp-rs#1](https://github.com/bradsjm/mail-imap-mcp-rs/issues/1) — `FETCH RFC822` empty, `BODY[]` works. Curl’s URL fetch uses `BODY[]`.

## Next zsh test

`retrieve_mail_applecurl.py` — standalone. IMAP is **`/usr/bin/curl` only**. LIST/SEARCH stay on the mailbox URL + custom request (33 folders already). Bodies use the documented URL shape `imaps://imap.mail.me.com:993/Archive;UID=n` — **no** `-X FETCH`, **no** `;PEEK=`. That exact URL has **not** been run in their zsh.

Curl’s native fetch uses `BODY[]` and may set `\Seen`. The script reads FLAGS first (line-based, no literal). If the message was unseen, it sends `UID STORE -FLAGS.SILENT (\Seen)` after a successful download. Mail is not moved. Password: `IMAP_APP_PASSWORD` or getpass — never a file, never curl argv.

Copy the file to Desktop:

```bash
# read -s IMAP_APP_PASSWORD && export IMAP_APP_PASSWORD
/usr/bin/python3 ~/Desktop/retrieve_mail_applecurl.py --list-only
/usr/bin/python3 ~/Desktop/retrieve_mail_applecurl.py --max-messages 1
```

`--list-only` should match the curl LIST they already saw.
`--max-messages 1` is the remaining test: one JSONL row with non-empty `text`/`raw`.
Do not treat retrieve as done until that zsh run writes that row.

Output: `~/Desktop/icloud_mail_all.jsonl`.

## Other scripts

| File | Role |
| --- | --- |
| `retrieve_mail_applecurl.py` | **Next zsh test.** Apple curl URL `mailbox;UID=` (curl#10076). |
| `retrieve_mail_openssl.py` | Failed in owner zsh (openssl connect errno 9). |
| `retrieve_mail_imaplib.py` | Failed in owner zsh (Python EBADF). |
| `retrieve_mail_curl.py` | Old custom-FETCH path (0-byte bodies). CLI points at applecurl. |
| `icloud_mail.py` | Aug 13 filer. Dry-run unless `--apply`. |
| `retrieve_mail.py` | Failed imap-tools retrieve. |
| `move_icloud_junk.py` | Dry-run junk mover. `--apply` required. |

## JSONL record

`folder`, `uid`, `flags`, `internaldate`, `date`, `from`, `to`, `cc`, `subject`,
`message_id`, `text`, `html`, `rfc822_size`, `raw`.
