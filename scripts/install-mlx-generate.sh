#!/bin/bash
# One-command MBP install/load for Mailroom generate (PROCESS=mlx_lm.server).
# Copies SoR generate files into $HOME/MailArchive, stages the LaunchAgent
# with real $HOME paths, bootstraps, kickstarts (RunAtLoad is false), and
# curls GET /v1/models.
#
# Generate-down is `bootout` / `down` — not kill (KeepAlive would restart).
# Do not open LM Studio.app. Do not start generate with lms. Do not overwrite
# ask_mail.py with a stub (HARD DECK).
#
# Usage:
#   ./scripts/install-mlx-generate.sh              # stage + load + verify
#   ./scripts/install-mlx-generate.sh install
#   ./scripts/install-mlx-generate.sh stage        # files only
#   ./scripts/install-mlx-generate.sh load         # bootstrap + kickstart
#   ./scripts/install-mlx-generate.sh down         # bootout, not kill
#   ./scripts/install-mlx-generate.sh status
#
# Env:
#   MAILARCHIVE                 default $HOME/MailArchive
#   MAILROOM_LAUNCH_AGENTS      default $HOME/Library/LaunchAgents
#   MAILROOM_REPO               override SoR checkout (parent of scripts/)
#   MAILROOM_INSTALL_SKIP_LAUNCHCTL=1   stage without launchctl
#   MAILROOM_INSTALL_SKIP_VERIFY=1      skip curl /v1/models
#   MAILROOM_INSTALL_VERIFY_TIMEOUT     seconds, default 90
set -euo pipefail

LABEL="com.mailroom.mlx-generate"
MODELS_URL="http://127.0.0.1:1234/v1/models"

here="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$here/../launchd/${LABEL}.plist" ]; then
  ROOT="$(cd "$here/.." && pwd)"
elif [ -n "${MAILROOM_REPO:-}" ] && [ -f "${MAILROOM_REPO}/launchd/${LABEL}.plist" ]; then
  ROOT="$(cd "$MAILROOM_REPO" && pwd)"
else
  echo "install-mlx-generate: cannot find launchd/${LABEL}.plist next to scripts/" >&2
  echo "set MAILROOM_REPO to the koolkurkle checkout" >&2
  exit 2
fi

HOME_DIR="${HOME:?HOME is required}"
MAILARCHIVE="${MAILARCHIVE:-$HOME_DIR/MailArchive}"
AGENTS_DIR="${MAILROOM_LAUNCH_AGENTS:-$HOME_DIR/Library/LaunchAgents}"
DEST_PLIST="$AGENTS_DIR/${LABEL}.plist"
SRC_PLIST="$ROOT/launchd/${LABEL}.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
TIMEOUT="${MAILROOM_INSTALL_VERIFY_TIMEOUT:-90}"

SCRIPT_FILES=(
  mlx-generate-server.sh
  ask_mail.py
  mailroom_generate.py
  ask_mail_generate_probes.py
  install-mlx-generate.sh
)
DOC_FILES=(
  generate-mlx.md
  ask_mail.md
  model-runtime-gates.md
)

usage() {
  cat <<'EOF'
install-mlx-generate — MBP generate LaunchAgent (PROCESS=mlx_lm.server)

  install   copy scripts+docs, stage plist, bootstrap, kickstart, curl /v1/models
  stage     copy scripts+docs and stage plist only (no launchctl)
  load      bootstrap + kickstart already-staged plist (RunAtLoad is false)
  down      launchctl bootout — NOT kill (KeepAlive would restart)
  status    print dest paths + listener + GET /v1/models
  help      this text

VERIFY_CMDS (operator):

  curl -sS http://127.0.0.1:1234/v1/models
  lsof -nP -iTCP:1234 -sTCP:LISTEN || echo ":1234 free"

Generate-down:

  ./scripts/install-mlx-generate.sh down
  # equivalent:
  launchctl bootout "gui/$(id -u)/com.mailroom.mlx-generate"

Do not kill the mlx PID. KeepAlive is true.
EOF
}

refuse_stub() {
  local src="$1"
  if [ ! -f "$src" ]; then
    echo "HARD DECK: missing $src" >&2
    exit 2
  fi
  if grep -q "LOADED_FROM_MCP_PUSH_ASK_JSON" "$src" 2>/dev/null; then
    echo "HARD DECK: refusing stub ask_mail.py (MCP placeholder)" >&2
    exit 2
  fi
  local bytes
  bytes="$(wc -c < "$src" | tr -d ' ')"
  if [ "$bytes" -lt 10000 ]; then
    echo "HARD DECK: refusing stub ask_mail.py (size ${bytes})" >&2
    exit 2
  fi
  if ! grep -q "def ask(" "$src"; then
    echo "HARD DECK: refusing ask_mail.py without def ask(" >&2
    exit 2
  fi
}

copy_file() {
  local src="$1"
  local dest="$2"
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
}

stage() {
  refuse_stub "$ROOT/scripts/ask_mail.py"
  mkdir -p "$MAILARCHIVE/scripts" "$MAILARCHIVE/docs" "$MAILARCHIVE/launchd" \
    "$MAILARCHIVE/logs" "$AGENTS_DIR"

  local name
  for name in "${SCRIPT_FILES[@]}"; do
    if [ ! -f "$ROOT/scripts/$name" ]; then
      echo "missing $ROOT/scripts/$name" >&2
      exit 2
    fi
    copy_file "$ROOT/scripts/$name" "$MAILARCHIVE/scripts/$name"
  done
  chmod +x "$MAILARCHIVE/scripts/mlx-generate-server.sh" \
    "$MAILARCHIVE/scripts/install-mlx-generate.sh" \
    "$MAILARCHIVE/scripts/ask_mail.py" \
    "$MAILARCHIVE/scripts/ask_mail_generate_probes.py"

  for name in "${DOC_FILES[@]}"; do
    if [ -f "$ROOT/docs/$name" ]; then
      copy_file "$ROOT/docs/$name" "$MAILARCHIVE/docs/$name"
    fi
  done

  copy_file "$SRC_PLIST" "$MAILARCHIVE/launchd/${LABEL}.plist"
  if ! grep -q "__HOME__" "$SRC_PLIST"; then
    echo "plist template missing __HOME__ (launchd does not expand \$HOME)" >&2
    exit 2
  fi
  sed "s|__HOME__|${HOME_DIR}|g" "$SRC_PLIST" > "$DEST_PLIST"
  if grep -q "__HOME__" "$DEST_PLIST"; then
    echo "staged plist still has __HOME__" >&2
    exit 2
  fi

  echo "PROCESS=mlx_lm.server"
  echo "staged scripts $MAILARCHIVE/scripts"
  echo "staged docs    $MAILARCHIVE/docs"
  echo "staged plist   $DEST_PLIST"
}

have_launchctl() {
  command -v launchctl >/dev/null 2>&1
}

skip_launchctl() {
  [ "${MAILROOM_INSTALL_SKIP_LAUNCHCTL:-}" = "1" ]
}

load() {
  if skip_launchctl; then
    echo "skip launchctl (MAILROOM_INSTALL_SKIP_LAUNCHCTL=1)"
    return 0
  fi
  if ! have_launchctl; then
    echo "launchctl not on PATH — staged only. On MBP run: $0 load" >&2
    return 0
  fi
  if [ ! -f "$DEST_PLIST" ]; then
    echo "missing staged plist $DEST_PLIST — run stage first" >&2
    exit 2
  fi
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  if ! launchctl bootstrap "$DOMAIN" "$DEST_PLIST"; then
    launchctl load "$DEST_PLIST"
  fi
  # RunAtLoad is false — start now.
  launchctl kickstart -k "${DOMAIN}/${LABEL}" 2>/dev/null || launchctl start "$LABEL"
  echo "bootstrapped ${DOMAIN}/${LABEL} (KeepAlive; generate-down is bootout)"
}

down() {
  if skip_launchctl; then
    echo "skip launchctl bootout (MAILROOM_INSTALL_SKIP_LAUNCHCTL=1)"
    echo "generate-down on MBP: launchctl bootout gui/\$(id -u)/${LABEL}"
    echo "do not kill the mlx PID (KeepAlive would restart)"
    return 0
  fi
  if ! have_launchctl; then
    echo "launchctl not on PATH — cannot bootout here" >&2
    echo "on MBP: launchctl bootout gui/\$(id -u)/${LABEL}" >&2
    echo "do not kill the mlx PID (KeepAlive would restart)" >&2
    return 0
  fi
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || launchctl unload "$DEST_PLIST"
  echo "generate-down: bootout ${DOMAIN}/${LABEL} (not kill; KeepAlive)"
}

verify() {
  if [ "${MAILROOM_INSTALL_SKIP_VERIFY:-}" = "1" ]; then
    echo "skip verify (MAILROOM_INSTALL_SKIP_VERIFY=1)"
    echo "VERIFY: curl -sS ${MODELS_URL}"
    return 0
  fi
  local i=0
  while [ "$i" -lt "$TIMEOUT" ]; do
    if curl -sf "$MODELS_URL" >/dev/null 2>&1; then
      echo "GET ${MODELS_URL} ok"
      curl -sS "$MODELS_URL" || true
      return 0
    fi
    sleep 2
    i=$((i + 2))
  done
  echo "VERIFY FAIL: ${MODELS_URL} not ready after ${TIMEOUT}s" >&2
  echo "still: curl -sS ${MODELS_URL}" >&2
  echo "generate-down remains: $0 down  (bootout, not kill)" >&2
  return 1
}

status() {
  echo "PROCESS=mlx_lm.server"
  echo "HOME=$HOME_DIR"
  echo "MAILARCHIVE=$MAILARCHIVE"
  echo "DEST_PLIST=$DEST_PLIST"
  echo "LABEL=${DOMAIN}/${LABEL}"
  echo "KeepAlive=true  generate-down=bootout (not kill)"
  if have_launchctl && ! skip_launchctl; then
    launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null || launchctl list | grep -F "$LABEL" || true
  fi
  curl -sS "$MODELS_URL" 2>/dev/null || echo "GET ${MODELS_URL} not listening"
}

cmd="${1:-install}"
case "$cmd" in
  -h|--help|help) usage; exit 0 ;;
  install) stage; load; verify ;;
  stage) stage ;;
  load) load ;;
  down|bootout|unload) down ;;
  status) status ;;
  *)
    echo "unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
