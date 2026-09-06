#!/bin/zsh
# macos-slim INSTALL-ROOT — Heavy packet 20260905-02
# mdutil helper 700 root:wheel → /usr/local/libexec/
# plist 644 → /Library/LaunchDaemons/; bootstrap system domain
# optional sudoers.d from USERNAME template
#
# Stage resolution (first match with helper + plist):
#   1. $1 or $MACOS_SLIM_ROOT_STAGE
#   2. this script's directory (repo: macos-slim/root/)
#   3. live Mini: ~/Library/Scripts/macos-slim/root-stage
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HELPER_DEST="/usr/local/libexec/macos-slim-root.sh"
PLIST_DEST="/Library/LaunchDaemons/com.user.macos-slim-root.plist"
SUDOERS_DEST="/etc/sudoers.d/macos-slim"

login_home() {
  if [ -n "${MACOS_SLIM_LOGIN_HOME:-}" ]; then
    printf '%s' "$MACOS_SLIM_LOGIN_HOME"
    return
  fi
  u="${SUDO_USER:-${USER:-}}"
  if [ -n "$u" ] && [ "$u" != root ] && [ -d "/Users/$u" ]; then
    printf '%s' "/Users/$u"
    return
  fi
  printf '%s' "${HOME}"
}

mini_stage() {
  printf '%s' "$(login_home)/Library/Scripts/macos-slim/root-stage"
}

stage_ok() {
  [ -n "$1" ] && [ -f "$1/macos-slim-root.sh" ] && [ -f "$1/com.user.macos-slim-root.plist" ]
}

resolve_stage() {
  if [ -n "${1:-}" ]; then
    printf '%s' "$1"
    return
  fi
  if [ -n "${MACOS_SLIM_ROOT_STAGE:-}" ]; then
    printf '%s' "$MACOS_SLIM_ROOT_STAGE"
    return
  fi
  if stage_ok "$HERE"; then
    printf '%s' "$HERE"
    return
  fi
  ms="$(mini_stage)"
  if stage_ok "$ms"; then
    printf '%s' "$ms"
    return
  fi
  printf ''
}

STAGE="$(resolve_stage "${1:-}")"
if ! stage_ok "${STAGE:-}"; then
  printf 'error: no macos-slim root stage (helper + plist)\n' >&2
  printf '  repo:   %s\n' "$HERE" >&2
  printf '  Mini:   %s\n' "$(mini_stage)" >&2
  printf '  or pass a stage dir / MACOS_SLIM_ROOT_STAGE\n' >&2
  exit 2
fi

HELPER_SRC="$STAGE/macos-slim-root.sh"
PLIST_SRC="$STAGE/com.user.macos-slim-root.plist"
SUDOERS_SRC="$STAGE/macos-slim.sudoers.example"

if [ "${MACOS_SLIM_ROOT_DRY:-}" = 1 ]; then
  printf 'stage=%s\n' "$STAGE"
  printf 'helper_src=%s\n' "$HELPER_SRC"
  printf 'plist_src=%s\n' "$PLIST_SRC"
  printf 'helper_dest=%s\n' "$HELPER_DEST"
  printf 'plist_dest=%s\n' "$PLIST_DEST"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  exec sudo --preserve-env=INSTALL_SUDOERS,MACOS_SLIM_ROOT_STAGE,MACOS_SLIM_LOGIN_HOME /bin/zsh "$0" "$@"
fi

/usr/bin/install -d -o root -g wheel -m 755 /usr/local/libexec
/usr/bin/install -o root -g wheel -m 700 "$HELPER_SRC" "$HELPER_DEST"
/usr/bin/install -o root -g wheel -m 644 "$PLIST_SRC" "$PLIST_DEST"

launchctl bootout system/com.user.macos-slim-root 2>/dev/null || true
launchctl bootstrap system "$PLIST_DEST"

if [ "${INSTALL_SUDOERS:-}" = 1 ]; then
  if [ ! -f "$SUDOERS_SRC" ]; then
    printf 'error: missing sudoers template: %s\n' "$SUDOERS_SRC" >&2
    exit 2
  fi
  user="${SUDO_USER:-${USER:-USERNAME}}"
  tmp="$(mktemp)"
  sed "s/USERNAME/${user}/g" "$SUDOERS_SRC" > "$tmp"
  /usr/bin/install -o root -g wheel -m 440 "$tmp" "$SUDOERS_DEST"
  rm -f "$tmp"
  printf 'installed sudoers: %s (user %s)\n' "$SUDOERS_DEST" "$user"
fi

printf 'installed %s (700 root:wheel) and %s (644) from %s; bootstrapped system domain\n' \
  "$HELPER_DEST" "$PLIST_DEST" "$STAGE"
