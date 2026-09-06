#!/bin/zsh
# Install macos-slim root helper (Heavy packet 20260905-02).
# Helper is mdutil-only. Optional sudoers.d from the USERNAME template.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HELPER_SRC="$HERE/macos-slim-root.sh"
PLIST_SRC="$HERE/com.user.macos-slim-root.plist"
HELPER_DEST="/usr/local/libexec/macos-slim-root.sh"
PLIST_DEST="/Library/LaunchDaemons/com.user.macos-slim-root.plist"
SUDOERS_SRC="$HERE/macos-slim.sudoers.example"
SUDOERS_DEST="/etc/sudoers.d/macos-slim"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo /bin/zsh "$0" "$@"
fi

if [ ! -f "$HELPER_SRC" ] || [ ! -f "$PLIST_SRC" ]; then
  printf 'error: missing helper or plist next to INSTALL-ROOT.sh\n' >&2
  exit 2
fi

/usr/bin/install -d -o root -g wheel -m 755 /usr/local/libexec
/usr/bin/install -o root -g wheel -m 700 "$HELPER_SRC" "$HELPER_DEST"
/usr/bin/install -o root -g wheel -m 644 "$PLIST_SRC" "$PLIST_DEST"

launchctl bootout system/com.user.macos-slim-root 2>/dev/null || true
launchctl bootstrap system "$PLIST_DEST"

if [ "${INSTALL_SUDOERS:-}" = 1 ]; then
  user="${SUDO_USER:-${USER:-USERNAME}}"
  tmp="$(mktemp)"
  sed "s/USERNAME/${user}/g" "$SUDOERS_SRC" > "$tmp"
  /usr/bin/install -o root -g wheel -m 440 "$tmp" "$SUDOERS_DEST"
  rm -f "$tmp"
  printf 'installed sudoers: %s (user %s)\n' "$SUDOERS_DEST" "$user"
fi

printf 'installed %s (700 root:wheel) and %s (644); bootstrapped system domain\n' \
  "$HELPER_DEST" "$PLIST_DEST"
