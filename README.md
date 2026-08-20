# koolkurkle

iCloud mail retrieve scripts for kirkbacon@me.com.

The owner's Mac can reach `imap.mail.me.com:993` with curl and nc (TLS + IMAP
greeting). Homebrew Python cannot: `MailBox("imap.mail.me.com").login(...)`
raises `OSError [Errno 9] Bad file descriptor` during socket connect, **before
LOGIN**. Pinning port 993, forcing IPv4 to `17.42.251.69`, and switching to
Python 3.11 did not fix it. That is a Python socket problem on that Mac, not a
bad app password.

**Use curl for retrieve-all on macOS.** Do not use the Python imap-tools
scripts for IMAP TCP/TLS on that machine.

## Retrieve every message on macOS

Password is read from the environment or a hidden prompt. It is never written
to a file. Mail is not marked read (`UID FETCH <uid> (BODY.PEEK[])` on the
mailbox URL — the same custom-request style as `UID SEARCH ALL`). Mail is not
moved. There is no `--apply` on this command.

`/usr/bin/curl` on the owner's Mac accepts `imaps://imap.mail.me.com:993/Archive`
plus a `request`, and rejects `…/Archive/;UID=96;PEEK=1` with curl (3) "URL
using bad/illegal format". Fetch no longer puts `/;UID=` or `;PEEK=` in the URL.

```bash
cd /path/to/koolkurkle

# Option A — env var in this terminal only (do not put this in a file):
#   read -s IMAP_APP_PASSWORD && export IMAP_APP_PASSWORD
#   printf '\n'
python3 retrieve_mail_curl.py

# Option B — omit the env var; the script prompts with getpass.
```

What that does:

- IMAP host `imap.mail.me.com` port 993, user `kirkbacon@me.com`
- Walks every selectable folder
- Writes `~/Desktop/icloud_mail_all.jsonl` (one JSON object per message)
- Talks IMAP only through **curl `imaps://`**. Python does not open the IMAP
  socket.
- If the JSONL already exists, new `folder+uid` rows are appended (resume).
  Pass `--overwrite` to replace the file.

Optional checks on the Mac (still curl, still no Python IMAP socket):

```bash
curl --version | grep -i imap          # need IMAP/IMAPS in the protocol list
python3 retrieve_mail_curl.py --list-only
python3 retrieve_mail_curl.py --max-messages 1   # one message, then stop
```

If Apple `/usr/bin/curl` was built without IMAP, install Homebrew curl and
point at it (this is still not Python sockets):

```bash
export CURL_BIN=/opt/homebrew/opt/curl/bin/curl
python3 retrieve_mail_curl.py
```

Override the JSONL path with `--output` if needed. Default is
`~/Desktop/icloud_mail_all.jsonl`.

This repo does **not** claim the Linux cloud agent talking to iCloud IMAP
proves the Mac path. The Mac failure is Python EBADF; the documented command
avoids that stack.

## Desktop scripts (Python IMAP — broken on that Mac)

These match the existing Desktop scripts. They use `imap-tools`
`MailBox().login()`. They will fail with EBADF on the current Homebrew Python.
They do not write the app password to a file. Password: `IMAP_APP_PASSWORD` or
getpass.

| File | Role |
| --- | --- |
| `icloud_mail.py` | Aug 13 INBOX filer → `Receipts` / `Has Attachments` / `Old Unsubscribe`. Dry-run unless `--apply`. |
| `retrieve_mail.py` | Retrieve-all via Python sockets. Includes the failed `MailBoxIPv4` pin and 3.14 hard-exit. |
| `move_icloud_junk.py` | Dry-run junk mover. Reads `~/Desktop/icloud_mail_junk.jsonl` `{folder, uid}`. `--apply` required. |

```bash
python3 -m pip install -r requirements.txt   # only for the three scripts above
python3 icloud_mail.py                       # dry-run
python3 move_icloud_junk.py                  # dry-run; no --apply
```

Do not run `--apply` unless you intend to move mail. The retrieve-all command
in the first section never moves mail.

## JSONL record

Each line is a JSON object with `folder`, `uid`, `flags`, `internaldate`,
`date`, `from`, `to`, `cc`, `subject`, `message_id`, `text`, `html`,
`rfc822_size`, and `raw` (full RFC822, latin-1-safe).
