# mailroom — iCloud IMAP body fetch (macOS)

Headers-only IMAP already works with Apple `/usr/bin/curl` (`UID SEARCH`,
`ENVELOPE`, `UID MOVE`). **Full `BODY.PEEK[]` does not** on that binary.

## Why Apple curl truncates

Apple `/usr/bin/curl` **8.7.1** (2024-03-27) treats custom `-X UID FETCH`
as LIST/SEARCH and does not stream IMAP `{size}` literals
([curl#18847](https://github.com/curl/curl/issues/18847)). Observed:

```text
truncated FETCH literal: got 5 of 26973 bytes
```

The fix shipped in **curl 8.17.0** (`imap: fix custom FETCH commands to
handle literal responses`). Homebrew curl is keg-only (not linked over
`/usr/bin/curl`) and is new enough.

Python `imaplib` / sockets to `imap.mail.me.com` fail `EBADF` on the
owner Mac. **Never use them.** This script only orchestrates curl.

## Install Homebrew curl (Apple Silicon)

```bash
brew install curl
export CURL_BIN="$(brew --prefix curl)/bin/curl"
# exact default path:
#   /opt/homebrew/opt/curl/bin/curl
"$CURL_BIN" --version
# expect 8.17.0+ and "Protocols: ... imap imaps ..."
```

Intel Homebrew prefix is `/usr/local/opt/curl/bin/curl`.

Do **not** point `CURL_BIN` at `/usr/bin/curl` for body download.
`--dry-run` / `--list-only` / `--probe-curl` may use Apple curl; body
fetch refuses any curl older than 8.17.0.

Optional fallback (marks `\Seen`; not PEEK): `--fetch-mode uid-url`
uses curl's native `imaps://host/mailbox;UID=n` URL (`BODY[]`). Prefer
Homebrew + `BODY.PEEK[]`.

## Copy onto the Mac

```bash
mkdir -p ~/MailArchive/scripts
cp mailroom/imap_fetch_bodies.py ~/MailArchive/scripts/
```

From a clone:

```bash
mkdir -p ~/MailArchive/scripts
cp /path/to/koolkurkle/mailroom/imap_fetch_bodies.py ~/MailArchive/scripts/
```

## Run (zsh)

Password is read from `IMAP_APP_PASSWORD` or a prompt. It is passed to
curl via `-K` stdin only — never argv, never a file.

```bash
export CURL_BIN="$(brew --prefix curl)/bin/curl"
export IMAP_USER='you@example.com'   # placeholder — your iCloud IMAP user
# read -s IMAP_APP_PASSWORD && export IMAP_APP_PASSWORD

cd ~/MailArchive/scripts
/usr/bin/python3 imap_fetch_bodies.py --probe-curl
/usr/bin/python3 imap_fetch_bodies.py --dry-run
/usr/bin/python3 imap_fetch_bodies.py --dry-run --folder INBOX
/usr/bin/python3 imap_fetch_bodies.py --max-messages 1
```

Default output: `~/MailArchive/bodies.jsonl` (resume-safe; folder+uid).
Each row has `text` (and `subject` / `from`) for a later sqlite FTS
index. Use `--no-raw` to omit the RFC822 blob.

```bash
/usr/bin/python3 imap_fetch_bodies.py --folder INBOX --no-raw
```

## Tests (no network)

From the repo root:

```bash
/usr/bin/python3 -m unittest tests.test_imap_fetch_bodies
```
