# IMAP curl binaries (macOS)

Headers-only and BODY.PEEK use **different** curl binaries. Do not point
one path at the other.

## Headers-only (8pm / imap_newmail / imap_tombstone)

Default is Apple `/usr/bin/curl`. Homebrew curl often fails
`imap.mail.me.com:993` with `EBADF` / Bad file descriptor on LIST,
SEARCH, and ENVELOPE.

`CURL_BIN` is an override only if needed. The 8pm headers path does
**not** need `CURL_BIN=/usr/bin/curl`.

```bash
/usr/bin/python3 imap_tombstone.py --print-curl
# /usr/bin/curl
```

## BODY.PEEK body fetch

Full-message `BODY.PEEK[]` still needs Homebrew curl **≥ 8.17**
(`imap_fetch_bodies.py` / `imap_fetch_bodies_fts`). Apple `/usr/bin/curl`
8.7.1 truncates `{size}` literals ([curl#18847](https://github.com/curl/curl/issues/18847)).
Those scripts keep their own brew preference — do not change it.

```bash
export CURL_BIN="$(brew --prefix curl)/bin/curl"
# Apple Silicon: /opt/homebrew/opt/curl/bin/curl
```
