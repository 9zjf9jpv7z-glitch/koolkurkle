#!/bin/sh
# macos-slim-root — Heavy packet 20260905-02
# mdutil only. Does not launchctl any agents.
# apply  = mdutil -d on /, Data, and non-TM /Volumes
# restore = mdutil -i on the same
# boot   = read user mode; apply when slim or persist
# Skip Time Machine destinations via tmutil destinationinfo.
set -eu

STATE_LEAF="Library/Application Support/macos-slim"

console_user() {
  if [ -n "${MACOS_SLIM_USER:-}" ]; then
    printf '%s' "$MACOS_SLIM_USER"
    return
  fi
  if [ -e /dev/console ]; then
    stat -f '%Su' /dev/console 2>/dev/null || true
  fi
}

user_state_dir() {
  u="$(console_user)"
  if [ -n "$u" ] && [ "$u" != root ] && [ -d "/Users/$u" ]; then
    printf '%s' "/Users/$u/$STATE_LEAF"
    return
  fi
  for d in /Users/*/"$STATE_LEAF"; do
    if [ -f "$d/mode" ]; then
      printf '%s' "$d"
      return
    fi
  done
}

read_mode() {
  dir="$(user_state_dir)"
  if [ -n "$dir" ] && [ -f "$dir/mode" ]; then
    tr -d '[:space:]' < "$dir/mode"
  else
    printf '%s' off
  fi
}

is_tm_volume() {
  vol="$1"
  if ! command -v tmutil >/dev/null 2>&1; then
    return 1
  fi
  tmutil destinationinfo 2>/dev/null | awk -F': ' '
    /Mount Point/ {
      gsub(/^ +| +$/, "", $2)
      print $2
    }
  ' | grep -Fxq "$vol"
}

list_volumes() {
  printf '%s\n' /
  if [ -d /System/Volumes/Data ]; then
    printf '%s\n' /System/Volumes/Data
  fi
  for vol in /Volumes/*; do
    [ -e "$vol" ] || continue
    if is_tm_volume "$vol"; then
      continue
    fi
    printf '%s\n' "$vol"
  done
}

do_apply() {
  list_volumes | while IFS= read -r vol; do
    [ -n "$vol" ] || continue
    mdutil -d "$vol" >/dev/null 2>&1 || true
  done
}

do_restore() {
  list_volumes | while IFS= read -r vol; do
    [ -n "$vol" ] || continue
    mdutil -i on "$vol" >/dev/null 2>&1 || true
  done
}

do_boot() {
  mode="$(read_mode)"
  case "$mode" in
    slim|persist)
      do_apply
      ;;
  esac
}

cmd="${1:-}"
case "$cmd" in
  apply) do_apply ;;
  restore) do_restore ;;
  boot) do_boot ;;
  *)
    printf 'usage: macos-slim-root.sh apply|restore|boot\n' >&2
    exit 2
    ;;
esac
