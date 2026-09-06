#!/bin/zsh
# macos-slim restore — Heavy packet 20260905-02
# SIP-safe: launchctl enable + kickstart for the default slim set only.
# No csrutil, no sealed-volume mounts, no moving Apple plists out of /System.
set -euo pipefail

AGENTS=(
  com.apple.mediaanalysisd
  com.apple.photoanalysisd
  com.apple.photolibraryd
  com.apple.duetexpertd
)

ROOT_HELPER="${MACOS_SLIM_ROOT_HELPER:-/usr/local/libexec/macos-slim-root.sh}"

if [ -x "$ROOT_HELPER" ]; then
  "$ROOT_HELPER" restore || true
elif [ -e "$ROOT_HELPER" ]; then
  sudo -n "$ROOT_HELPER" restore 2>/dev/null || true
fi

uid="$(id -u)"
domain="gui/${uid}"

for label in "${AGENTS[@]}"; do
  launchctl enable "${domain}/${label}" 2>/dev/null || true
  launchctl kickstart -k "${domain}/${label}" 2>/dev/null || true
done
