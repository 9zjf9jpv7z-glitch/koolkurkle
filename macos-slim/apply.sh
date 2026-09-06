#!/bin/zsh
# macos-slim apply — Heavy packet 20260905-02
# SIP-safe: launchctl disable + bootout + kill SIGINT only.
# No csrutil, no sealed-volume mounts, no moving Apple plists out of /System.
# Does not touch softwareupdated / XProtect / syspolicyd / MRT / Find My /
# WindowServer / FileVault / firewall.
# Does not touch coreduetd / dasd / suggestd / sharingd / rapportd / useractivityd.
set -euo pipefail

AGENTS=(
  com.apple.mediaanalysisd
  com.apple.photoanalysisd
  com.apple.photolibraryd
  com.apple.duetexpertd
)

ROOT_HELPER="${MACOS_SLIM_ROOT_HELPER:-/usr/local/libexec/macos-slim-root.sh}"

if [ -x "$ROOT_HELPER" ]; then
  "$ROOT_HELPER" apply || true
elif [ -e "$ROOT_HELPER" ]; then
  sudo -n "$ROOT_HELPER" apply 2>/dev/null || true
fi

uid="$(id -u)"
domain="gui/${uid}"

for label in "${AGENTS[@]}"; do
  launchctl disable "${domain}/${label}" 2>/dev/null || true
  launchctl bootout "${domain}/${label}" 2>/dev/null || true
  launchctl kill SIGINT "${domain}/${label}" 2>/dev/null || true
done
