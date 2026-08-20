# koolkurkle

iCloud mail retrieve scripts for kirkbacon@me.com.

## What ran where (do not mix these up)

| Check | Where | Result |
| --- | --- | --- |
| Apple `/usr/bin/curl` 8.7.1 LIST (33 folders) | Owner **zsh** | Succeeded |
| Apple curl FETCH message bodies | Owner **zsh** | Failed: `{size}` line, **0 bytes** of RFC822 (`N` up to 14941242). `;PEEK=1` URLs were curl (3). |
| `/usr/bin/python3` → CLT 3.9 `retrieve_mail_imaplib.py --list-only` | Owner **zsh** | Failed: `IMAP4_SSL` → `socket.create_connection` → `OSError [Errno 9] EBADF` in `sock.connect`, **before LOGIN** |
| `/usr/bin/python3` 3.9.6 `create_connection` to `imap.mail.me.com:993` | **Another agent process** on that Mac, not this zsh | Reported success (`17.156.192.7`). **Not proven in the owner's Terminal.** |
| Homebrew 3.11 and `~/.venv` 3.14 EBADF on IMAP connect | That other agent process | Failed. Not re-checked in this zsh; do not rely on those Pythons. |
| curl / nc to port 993; LIST with a good app password | Owner **zsh** | Reachable. LIST success means this is not a bad password. |

A Linux cloud VM talking to IMAP is not proof. An agent `create_connection` is not a zsh result.

In the owner's Terminal, **Python sockets are not a working IMAP transport**. Curl can speak enough IMAP to LIST, but it does not download FETCH literals.

## Retrieve on macOS (next Terminal test)

TLS and IMAP bytes go through **`/usr/bin/openssl s_client`**. Python only prompts, parses, and writes JSONL. Password is `IMAP_APP_PASSWORD` or getpass — never a file, never openssl argv.

Mailboxes: `EXAMINE` (read-only). Bodies: `UID FETCH … (BODY.PEEK[])`. Nothing is moved. No `--apply`.

Copy `retrieve_mail_openssl.py` to the Mac if you run from Desktop:

```bash
# read -s IMAP_APP_PASSWORD && export IMAP_APP_PASSWORD
/usr/bin/python3 ~/Desktop/retrieve_mail_openssl.py --list-only
/usr/bin/python3 ~/Desktop/retrieve_mail_openssl.py --max-messages 1
```

From a repo checkout, same commands with that file path. Full retrieve (after those two succeed):

```bash
/usr/bin/python3 retrieve_mail_openssl.py
```

Output: `~/Desktop/icloud_mail_all.jsonl` (`--output` to override). Resume by `folder+uid` unless `--overwrite`.

`--list-only` is the next Mac test for openssl LOGIN + LIST.
`--max-messages 1` is the next Mac test for a real FETCH literal (`text`/`raw` non-empty).
Do not treat retrieve as done until that zsh run writes one real JSONL row.

## Other scripts in this repo

| File | Role |
| --- | --- |
| `retrieve_mail_openssl.py` | **Next Mac retrieve test.** openssl s_client IMAP. |
| `retrieve_mail_imaplib.py` | Failed in owner zsh (Python EBADF before LOGIN). |
| `retrieve_mail_curl.py` | LIST worked in zsh; FETCH literals did not. CLI points at the openssl script. |
| `icloud_mail.py` | Aug 13 INBOX filer. Dry-run unless `--apply`. |
| `retrieve_mail.py` | Failed imap-tools retrieve (`MailBoxIPv4` + 3.14 hard-exit). |
| `move_icloud_junk.py` | Dry-run junk mover. `--apply` required. |

## JSONL record

Each line: `folder`, `uid`, `flags`, `internaldate`, `date`, `from`, `to`, `cc`,
`subject`, `message_id`, `text`, `html`, `rfc822_size`, `raw` (RFC822, latin-1-safe).
