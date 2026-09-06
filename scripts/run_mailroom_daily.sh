#!/bin/zsh
# Mini daily RAG LaunchAgent entry (mac-mini.local; paths from $HOME / MAILARCHIVE).
#
# Headers IMAP uses Apple /usr/bin/curl via mailroom_daily.py.
# Body/FTS scripts pick Homebrew curl themselves (CURL_BIN unset for that step).
# Embed uses ~/MailArchive/.venv/bin/python — not Apple /usr/bin/python3.
#
# Keychain item name only: mailroom.icloud.app-password
# Never echo, log, or commit the password.
#
# zsh on Mac (also runs under bash 3.2+ with the same builtins).
set -eu

MAILARCHIVE="${MAILARCHIVE:-$HOME/MailArchive}"
SCRIPTS="${MAILARCHIVE_SCRIPTS:-$MAILARCHIVE/scripts}"
LOGS="${MAILARCHIVE_LOGS:-$MAILARCHIVE/logs}"
mkdir -p "$LOGS"

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin${PATH:+:$PATH}"
export PYTHONUNBUFFERED=1
export MAILARCHIVE
export MAILARCHIVE_SCRIPTS="$SCRIPTS"
export MAILARCHIVE_LOGS="$LOGS"

# Load IMAP app password from Keychain by service name only.
# Override the item with MAILROOM_KEYCHAIN_ITEM. Do not print the value.
KEYCHAIN_ITEM="${MAILROOM_KEYCHAIN_ITEM:-mailroom.icloud.app-password}"
if [ -z "${IMAP_APP_PASSWORD:-}" ] && [ -x /usr/bin/security ]; then
  set +e
  _pw="$(/usr/bin/security find-generic-password -s "$KEYCHAIN_ITEM" -w 2>/dev/null)"
  _rc=$?
  set -e
  if [ "$_rc" -eq 0 ] && [ -n "${_pw:-}" ]; then
    IMAP_APP_PASSWORD="$_pw"
    export IMAP_APP_PASSWORD
  fi
  unset _pw _rc
fi

DAILY_PY="$SCRIPTS/mailroom_daily.py"
if [ ! -f "$DAILY_PY" ]; then
  echo "error: missing $DAILY_PY" >&2
  exit 2
fi

APPLE_PY="${MAILROOM_APPLE_PY:-/usr/bin/python3}"
if [ ! -x "$APPLE_PY" ]; then
  APPLE_PY="$(command -v python3 || true)"
fi
if [ -z "$APPLE_PY" ]; then
  echo "error: no python3 for mailroom_daily.py" >&2
  exit 2
fi

# Fresh stamp → mailroom_daily.py exits 0 with no output (RunAtLoad catch-up).
# Step lines go to stderr (LaunchAgent StandardErrorPath) and the dated log.
exec "$APPLE_PY" "$DAILY_PY" --skip-if-fresh "$@"
