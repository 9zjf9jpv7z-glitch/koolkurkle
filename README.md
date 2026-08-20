# koolkurkle

iCloud mail retrieve scripts for kirkbacon@me.com.

## What is proven on the owner's Mac vs untested

| Check | Result |
| --- | --- |
| `/usr/bin/python3` 3.9.6 `socket.create_connection(("imap.mail.me.com", 993))` | Proven: connected (`17.156.192.7:993`). Also reached `1.1.1.1:443`. |
| `/usr/bin/python3` `imaplib` LOGIN and UID FETCH | **Untested.** Same interpreter that opened the socket; login/fetch have not been run. |
| `~/.venv` Python 3.14.7 + imap-tools | Proven fail: `OSError [Errno 9] EBADF` on IMAP connect, before LOGIN. |
| `/opt/homebrew/bin/python3.11` `create_connection` to IMAP (and pip/HTTPS) | Proven fail: same EBADF. |
| `/usr/bin/curl` 8.7.1 `retrieve_mail_curl.py --list-only` | Proven: 33 folders. |
| Apple curl FETCH of message bodies | Proven fail: no RFC822 after the IMAP `{size}` line (`truncated FETCH literal: got 0 of N`). `;PEEK=1` URLs were curl (3). |
| curl / nc to port 993 | Proven: reachable. LIST success means this is not a bad password. |

A Linux cloud VM connecting to IMAP is not proof of the Mac path.

## Retrieve every message on macOS

Use **Apple `/usr/bin/python3`**. Do **not** use `~/.venv` or Homebrew Python.

Password is `IMAP_APP_PASSWORD` or a getpass prompt. It is never written to a
file. Mailboxes are opened read-only (`select(..., readonly=True)` / EXAMINE).
Bodies are `UID FETCH … (BODY.PEEK[])`. Mail is not moved. No `--apply`.

```bash
cd /path/to/koolkurkle

# Option A — env var in this terminal only (do not put this in a file):
#   read -s IMAP_APP_PASSWORD && export IMAP_APP_PASSWORD
#   printf '\n'
/usr/bin/python3 retrieve_mail_imaplib.py --list-only
/usr/bin/python3 retrieve_mail_imaplib.py --max-messages 1
/usr/bin/python3 retrieve_mail_imaplib.py
```

Option B: omit the env var; the script prompts with getpass.

What the full command does once login/fetch work:

- Host `imap.mail.me.com:993`, user `kirkbacon@me.com`
- Every selectable folder
- `~/Desktop/icloud_mail_all.jsonl` (override with `--output`)
- Resume by `folder+uid` if the file already exists (`--overwrite` to replace)

`--list-only` is the remaining Mac test for **login + LIST**.
`--max-messages 1` is the remaining Mac test for **UID FETCH** (one real
`text`/`raw` body). Do not treat retrieve as done until that Mac run
writes a non-empty JSONL row.

## Desktop scripts (not the Mac retrieve path)

These use Homebrew-incompatible stacks (imap-tools on venv/Homebrew, or
Apple curl FETCH). Keep them; do not run them to retrieve mail on this Mac.

| File | Role |
| --- | --- |
| `retrieve_mail_imaplib.py` | **Mac retrieve.** `/usr/bin/python3` + stdlib imaplib. |
| `retrieve_mail_curl.py` | Failed fetch transport. LIST worked; FETCH literals did not. |
| `icloud_mail.py` | Aug 13 INBOX filer → Receipts / Has Attachments / Old Unsubscribe. Dry-run unless `--apply`. |
| `retrieve_mail.py` | Failed Python retrieve-all (`MailBoxIPv4` + 3.14 hard-exit). |
| `move_icloud_junk.py` | Dry-run junk mover. `{folder, uid}` JSONL. `--apply` required. |

```bash
python3 icloud_mail.py          # dry-run; needs imap-tools on a working Python
python3 move_icloud_junk.py     # dry-run; no --apply
```

## JSONL record

Each line is a JSON object with `folder`, `uid`, `flags`, `internaldate`,
`date`, `from`, `to`, `cc`, `subject`, `message_id`, `text`, `html`,
`rfc822_size`, and `raw` (full RFC822, latin-1-safe).
