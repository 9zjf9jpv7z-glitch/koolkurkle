#!/bin/zsh
# macos-slim — SIP-safe user controller (Heavy packet 20260905-02)
# Mini-only (Mac Mini M4 24GB / macOS Tahoe ~26.3). Do not install on MBP by default.
# SIP stays on: no csrutil, no sealed-volume mounts, no moving Apple plists out of /System.
#
# Default slim set (do not add coreduetd/dasd/suggestd/sharingd/rapportd/useractivityd):
#   com.apple.mediaanalysisd
#   com.apple.photoanalysisd
#   com.apple.photolibraryd
#   com.apple.duetexpertd
set -euo pipefail

# Resolve through ~/bin/macos-slim symlink to the real tree.
HERE="${0:A:h}"
LABEL="com.user.macos-slim"
STATE_DIR="${MACOS_SLIM_STATE:-$HOME/Library/Application Support/macos-slim}"
LOG="${MACOS_SLIM_LOG:-$HOME/Library/Logs/macos-slim.log}"
APPLY="${MACOS_SLIM_APPLY:-$HERE/apply.sh}"
RESTORE="${MACOS_SLIM_RESTORE:-$HERE/restore.sh}"
PLIST_TEMPLATE="$HERE/com.user.macos-slim.plist.template"
PLIST_DEST="${MACOS_SLIM_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
BIN_LINK="${MACOS_SLIM_BIN:-$HOME/bin/macos-slim}"
ZSHRC="${MACOS_SLIM_ZSHRC:-$HOME/.zshrc}"
PATH_LINE='export PATH="$HOME/bin:$PATH"'

AGENTS=(
  com.apple.mediaanalysisd
  com.apple.photoanalysisd
  com.apple.photolibraryd
  com.apple.duetexpertd
)

usage() {
  cat <<'EOF'
macos-slim — SIP-safe Mini agent slimming (Heavy 20260905-02)

  arm          slim until next reboot (tick applies)
  disarm       restore if applied; mode=off
  persist      slim across reboots (tick applies)
  restore-now  restore agents now; mode=off
  status       print mode, session, agents
  install      LaunchAgent + ~/bin link + PATH; mode=off if missing (does not apply)
  uninstall    bootout LaunchAgent; remove plist + ~/bin link
  tick         LaunchAgent entry (RunAtLoad + every 300s)

State: ~/Library/Application Support/macos-slim/{mode,session}
Default after install: mode=off. Do not arm/persist unless the operator at the machine asks.
EOF
}

log() {
  mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG" >/dev/null
  printf '%s\n' "$*"
}

read_mode() {
  if [ -f "$STATE_DIR/mode" ]; then
    tr -d '[:space:]' < "$STATE_DIR/mode"
  else
    printf '%s' off
  fi
}

read_session() {
  if [ -f "$STATE_DIR/session" ]; then
    tr -d '[:space:]' < "$STATE_DIR/session"
  fi
}

write_mode() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" > "$STATE_DIR/mode"
}

write_session() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" > "$STATE_DIR/session"
}

clear_session() {
  rm -f "$STATE_DIR/session"
}

current_session() {
  if [ -n "${MACOS_SLIM_SESSION:-}" ]; then
    printf '%s' "$MACOS_SLIM_SESSION"
  elif [ -x /usr/sbin/sysctl ]; then
    /usr/sbin/sysctl -n kern.bootsessionuuid 2>/dev/null | tr -d '[:space:]'
  else
    printf '%s' unknown
  fi
}

run_apply() {
  if [ -x "$APPLY" ]; then
    "$APPLY"
  else
    log "error: apply helper not executable: $APPLY"
    return 1
  fi
}

run_restore() {
  if [ -x "$RESTORE" ]; then
    "$RESTORE"
  else
    log "error: restore helper not executable: $RESTORE"
    return 1
  fi
}

cmd_tick() {
  mkdir -p "$STATE_DIR"
  mode="$(read_mode)"
  session="$(read_session)"
  now="$(current_session)"

  case "$mode" in
    armed)
      log "tick: armed → apply, mode=slim, session=$now"
      run_apply
      write_mode slim
      write_session "$now"
      ;;
    slim)
      if [ -n "$session" ] && [ "$session" = "$now" ]; then
        log "tick: slim + same session → re-apply"
        run_apply
      else
        log "tick: slim + new session → restore, mode=off"
        run_restore
        write_mode off
        clear_session
      fi
      ;;
    persist)
      log "tick: persist → apply, session=$now"
      run_apply
      write_session "$now"
      ;;
    *)
      # off or unknown — no-op
      ;;
  esac
}

cmd_arm() {
  write_mode armed
  log "mode=armed (slim until next reboot)"
  cmd_tick
}

cmd_disarm() {
  mode="$(read_mode)"
  case "$mode" in
    slim|persist|armed)
      if [ "$mode" != armed ]; then
        log "disarm: restore (was $mode)"
        run_restore
      else
        log "disarm: cancel pending arm (not yet applied)"
      fi
      ;;
  esac
  write_mode off
  clear_session
  log "mode=off"
}

cmd_persist() {
  write_mode persist
  log "mode=persist (slim across reboots)"
  cmd_tick
}

cmd_restore_now() {
  log "restore-now"
  run_restore
  write_mode off
  clear_session
  log "mode=off"
}

cmd_status() {
  mode="$(read_mode)"
  session="$(read_session)"
  now="$(current_session)"
  printf 'macos-slim Heavy 20260905-02\n'
  printf 'mode: %s\n' "$mode"
  printf 'session: %s\n' "${session:-}"
  printf 'boot: %s\n' "$now"
  printf 'state: %s\n' "$STATE_DIR"
  printf 'agents:\n'
  for label in "${AGENTS[@]}"; do
    printf '  %s\n' "$label"
  done
  root_helper="${MACOS_SLIM_ROOT_HELPER:-/usr/local/libexec/macos-slim-root.sh}"
  if [ -e "$root_helper" ]; then
    printf 'root helper: %s\n' "$root_helper"
  else
    printf 'root helper: not installed\n'
  fi
}

ensure_mode_off_if_missing() {
  if [ ! -f "$STATE_DIR/mode" ]; then
    write_mode off
  fi
}

# install generates the login LaunchAgent (template is the same shape).
generate_launchagent() {
  mkdir -p "$(dirname "$PLIST_DEST")" "$HOME/Library/Logs"
  slim_sh="${HERE}/macos-slim.sh"
  if [ -f "$PLIST_TEMPLATE" ]; then
    sed -e "s|__MACOS_SLIM_SH__|${slim_sh}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DEST"
  else
    cat > "$PLIST_DEST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.user.macos-slim</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/zsh</string>
		<string>${slim_sh}</string>
		<string>tick</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StartInterval</key>
	<integer>300</integer>
	<key>KeepAlive</key>
	<false/>
	<key>ProcessType</key>
	<string>Background</string>
	<key>StandardOutPath</key>
	<string>${HOME}/Library/Logs/macos-slim.stdout.log</string>
	<key>StandardErrorPath</key>
	<string>${HOME}/Library/Logs/macos-slim.stderr.log</string>
</dict>
</plist>
EOF
  fi
  chmod 644 "$PLIST_DEST"
}

bootstrap_gui_agent() {
  if [ "${MACOS_SLIM_SKIP_LAUNCHCTL:-}" = 1 ]; then
    return 0
  fi
  uid="$(id -u)"
  launchctl bootout "gui/${uid}/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/${uid}" "$PLIST_DEST"
  launchctl enable "gui/${uid}/${LABEL}"
}

cmd_install() {
  mkdir -p "$STATE_DIR" "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  chmod 700 "$HERE/macos-slim.sh" "$HERE/apply.sh" "$HERE/restore.sh" 2>/dev/null || true
  if [ -f "$HERE/root/macos-slim-root.sh" ]; then
    chmod 700 "$HERE/root/macos-slim-root.sh" 2>/dev/null || true
  fi
  if [ -f "$HERE/root/INSTALL-ROOT.sh" ]; then
    chmod 700 "$HERE/root/INSTALL-ROOT.sh" 2>/dev/null || true
  fi
  chmod 644 "$HERE/com.user.macos-slim.plist.template" 2>/dev/null || true
  if [ -f "$HERE/root/com.user.macos-slim-root.plist" ]; then
    chmod 644 "$HERE/root/com.user.macos-slim-root.plist" 2>/dev/null || true
  fi

  generate_launchagent
  ln -sfn "$HERE/macos-slim.sh" "$BIN_LINK"
  if [ ! -f "$ZSHRC" ] || ! grep -Fqs "$PATH_LINE" "$ZSHRC"; then
    printf '\n%s\n' "$PATH_LINE" >> "$ZSHRC"
    log "appended PATH line to $ZSHRC"
  fi
  ensure_mode_off_if_missing
  bootstrap_gui_agent
  log "install complete; mode=$(read_mode) (did not apply)"
}

cmd_uninstall() {
  mode="$(read_mode)"
  case "$mode" in
    slim|persist)
      log "uninstall: restore before removing agent"
      run_restore || true
      write_mode off
      clear_session
      ;;
  esac
  if [ "${MACOS_SLIM_SKIP_LAUNCHCTL:-}" != 1 ]; then
    uid="$(id -u)"
    launchctl bootout "gui/${uid}/${LABEL}" 2>/dev/null || true
  fi
  rm -f "$PLIST_DEST" "$BIN_LINK"
  log "uninstall complete (state dir left in place)"
}

cmd="${1:-}"
case "$cmd" in
  arm) cmd_arm ;;
  disarm) cmd_disarm ;;
  persist) cmd_persist ;;
  restore-now) cmd_restore_now ;;
  status) cmd_status ;;
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  tick) cmd_tick ;;
  -h|--help|help|"") usage ;;
  *)
    printf 'unknown command: %s\n' "$cmd" >&2
    usage >&2
    exit 2
    ;;
esac
