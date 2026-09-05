#!/bin/zsh
# Sample live MailArchive embed jobs (rem1 / rem3 / embed_shard*.latest).
#
# Prefer active shard logs over a finished embed_full run so a completed
# full backfill does not look like a soft-stall.
#
# zsh on Mac (also runs under bash 3.2+). No secrets.
#
# Usage:
#   embed_health_sample.sh
#   embed_health_sample.sh --pick-only
#   embed_health_sample.sh --json
#
# Env:
#   MAILARCHIVE_LOGS          default ~/MailArchive/logs
#   MAILARCHIVE_RUN           default ~/MailArchive/run
#   MAILARCHIVE_LAUNCHAGENTS  default ~/Library/LaunchAgents
#   EMBED_STALE_SEC           default 90

set -eu
if [ -n "${ZSH_VERSION:-}" ]; then
  setopt NULL_GLOB
fi

MODE="sample"
STALE_SEC="${EMBED_STALE_SEC:-90}"
LOGS_DIR="${MAILARCHIVE_LOGS:-$HOME/MailArchive/logs}"
RUN_DIR="${MAILARCHIVE_RUN:-$HOME/MailArchive/run}"
LAUNCH_DIR="${MAILARCHIVE_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
NOW_TS=""

usage() {
  echo "usage: $0 [--pick-only|--json] [--stale-sec N]" >&2
  echo "  follow live rem1/rem3/embed_shard*.latest; ignore finished embed_full" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pick-only) MODE="pick" ;;
    --json) MODE="json" ;;
    --stale-sec)
      shift
      STALE_SEC="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

# Portable epoch seconds (Apple date has no -d).
now_ts() {
  if [ -n "$NOW_TS" ]; then
    echo "$NOW_TS"
    return
  fi
  date +%s
}

# Portable mtime epoch. Mac stat -f %m; GNU stat -c %Y.
file_mtime() {
  if [ ! -e "$1" ]; then
    echo 0
    return
  fi
  if stat -f %m "$1" >/dev/null 2>&1; then
    stat -f %m "$1"
  else
    stat -c %Y "$1"
  fi
}

pid_alive() {
  [ -n "${1:-}" ] || return 1
  case "$1" in
    *[!0-9]*) return 1 ;;
  esac
  kill -0 "$1" 2>/dev/null
}

basename_of() {
  echo "${1##*/}"
}

# Job kind from a path or label: rem1 | rem3 | shard | full | other
job_kind() {
  _n=$(basename_of "$1" | tr '[:upper:]' '[:lower:]')
  case "$_n" in
    *rem3*) echo rem3 ;;
    *rem1*) echo rem1 ;;
    *embed_shard*|*shard*) echo shard ;;
    *embed_full*) echo full ;;
    *) echo other ;;
  esac
}

is_preferred_live_name() {
  _n=$(basename_of "$1" | tr '[:upper:]' '[:lower:]')
  case "$_n" in
    *dryrun*) return 1 ;;
    *rem1*|*rem3*|*embed_shard*|*shard*) return 0 ;;
    *.latest)
      case "$_n" in
        *embed*) return 0 ;;
      esac
      return 1
      ;;
    *) return 1 ;;
  esac
}

is_full_name() {
  _n=$(basename_of "$1" | tr '[:upper:]' '[:lower:]')
  case "$_n" in
    *embed_full*) return 0 ;;
    *) return 1 ;;
  esac
}

# Resolve a .latest symlink; otherwise echo the path.
resolve_log() {
  _p="$1"
  if [ -L "$_p" ]; then
    _dir=$(dirname "$_p")
    _tgt=$(readlink "$_p" || true)
    if [ -n "$_tgt" ]; then
      case "$_tgt" in
        /*) echo "$_tgt" ;;
        *) echo "$_dir/$_tgt" ;;
      esac
      return
    fi
  fi
  echo "$_p"
}

# Last "committed N/M" in the tail. Prints "N M" or empty.
read_committed() {
  _log="$1"
  [ -f "$_log" ] || return 0
  tail -c 262144 "$_log" 2>/dev/null | grep -E -i -o 'committed[[:space:]]+[0-9]+[[:space:]]*/[[:space:]]*[0-9]+' | tail -n 1 | awk '{
    gsub(/[^0-9]/, " ", $0)
    n=split($0, a, / +/)
    c=""; t=""
    for (i=1;i<=n;i++) {
      if (a[i] ~ /^[0-9]+$/) {
        if (c=="") c=a[i]
        else t=a[i]
      }
    }
    if (c!="" && t!="") print c, t
  }'
}

log_is_done() {
  _pair=$(read_committed "$1" || true)
  [ -n "$_pair" ] || return 1
  _c=${_pair%% *}
  _t=${_pair##* }
  [ "$_t" -gt 0 ] 2>/dev/null || return 1
  [ "$_c" -ge "$_t" ]
}

# Collect unique existing paths, one per line.
_add_path() {
  _p="$1"
  [ -e "$_p" ] || return 0
  case "$SEEN_PATHS" in
    *"|$_p|"*) return 0 ;;
  esac
  SEEN_PATHS="${SEEN_PATHS}${_p}|"
  CAND_PATHS="${CAND_PATHS}${_p}
"
}

SEEN_PATHS="|"
CAND_PATHS=""

collect_latest_symlinks() {
  [ -d "$LOGS_DIR" ] || return 0
  # Null-safe: no matches → empty.
  set +e
  for _p in "$LOGS_DIR"/*.latest "$LOGS_DIR"/embed_shard*.latest "$LOGS_DIR"/*rem1*.latest "$LOGS_DIR"/*rem3*.latest; do
    [ -e "$_p" ] || continue
    _add_path "$_p"
  done
  set -e
}

collect_full_logs() {
  [ -d "$LOGS_DIR" ] || return 0
  set +e
  for _p in "$LOGS_DIR"/embed_full*.log "$LOGS_DIR"/embed_full*; do
    [ -e "$_p" ] || continue
    case "$(basename_of "$_p")" in
      *dryrun*) continue ;;
    esac
    _add_path "$_p"
  done
  set -e
}

collect_shard_logs() {
  [ -d "$LOGS_DIR" ] || return 0
  set +e
  for _p in \
    "$LOGS_DIR"/embed_shard*.log \
    "$LOGS_DIR"/*rem1*.log \
    "$LOGS_DIR"/*rem3*.log \
    "$LOGS_DIR"/embed_shard* \
    "$LOGS_DIR"/*rem1* \
    "$LOGS_DIR"/*rem3*
  do
    [ -e "$_p" ] || continue
    case "$(basename_of "$_p")" in
      *dryrun*) continue ;;
    esac
    _add_path "$_p"
  done
  set -e
}

# Pid files: first line is a pid; optional second line is a log path.
# Names: embed_rem1.pid, rem1.pid, embed_shard.pid, *.pid next to rem/shard.
collect_pid_files() {
  for _dir in "$RUN_DIR" "$LOGS_DIR"; do
    [ -d "$_dir" ] || continue
    set +e
    for _pf in "$_dir"/*.pid; do
      [ -f "$_pf" ] || continue
      _bn=$(basename_of "$_pf")
      case "$_bn" in
        *rem1*|*rem3*|*embed_shard*|*shard*|*embed*) ;;
        *) continue ;;
      esac
      _pid=$(head -n 1 "$_pf" | tr -cd '0-9')
      [ -n "$_pid" ] || continue
      pid_alive "$_pid" || continue
      _log=$(sed -n '2p' "$_pf" | tr -d '\r')
      if [ -n "$_log" ] && [ -e "$_log" ]; then
        _add_path "$_log"
      elif [ -d "$LOGS_DIR" ]; then
        # Infer a sibling log from the pid basename.
        _stem=${_bn%.pid}
        for _guess in \
          "$LOGS_DIR/${_stem}.latest" \
          "$LOGS_DIR/${_stem}.log" \
          "$LOGS_DIR/embed_${_stem}.latest" \
          "$LOGS_DIR/embed_shard_${_stem}.latest"
        do
          if [ -e "$_guess" ]; then
            _add_path "$_guess"
            break
          fi
        done
      fi
    done
    set -e
  done
}

# LaunchAgents: plist Label / ProgramArguments mentioning rem1/rem3/embed_shard.
# If launchctl says the job is running, prefer its StandardOutPath / .latest.
collect_launchagents() {
  [ -d "$LAUNCH_DIR" ] || return 0
  command -v launchctl >/dev/null 2>&1 || return 0
  set +e
  for _plist in "$LAUNCH_DIR"/*.plist; do
    [ -f "$_plist" ] || continue
    _txt=$(tr '\n' ' ' < "$_plist")
    case "$_txt" in
      *rem1*|*rem3*|*embed_shard*|*embed_full*|*MailArchive*embed*) ;;
      *) continue ;;
    esac
    _label=$(sed -n 's/.*<key>Label<\/key>[[:space:]]*<string>\([^<]*\)<\/string>.*/\1/p' "$_plist" | head -n 1)
    _stdout=$(sed -n 's/.*<key>StandardOutPath<\/key>[[:space:]]*<string>\([^<]*\)<\/string>.*/\1/p' "$_plist" | head -n 1)
    _running=0
    if [ -n "$_label" ]; then
      if launchctl list "$_label" >/dev/null 2>&1; then
        _pid=$(launchctl list "$_label" 2>/dev/null | awk '/"PID"/{print $3}' | tr -cd '0-9')
        if pid_alive "$_pid"; then
          _running=1
        fi
      fi
    fi
    if [ "$_running" -eq 1 ]; then
      if [ -n "$_stdout" ] && [ -e "$_stdout" ]; then
        _add_path "$_stdout"
      fi
      # Also pick a matching .latest next to the stdout path or in logs.
      if is_preferred_live_name "$_plist" || is_preferred_live_name "$_label"; then
        collect_latest_symlinks
      fi
    fi
  done
  set -e
}

# Age of a log (resolved target if symlink).
log_age() {
  _src=$(resolve_log "$1")
  _mt=$(file_mtime "$_src")
  _now=$(now_ts)
  echo $((_now - _mt))
}

# A job is "live" if: pid alive OR (not done AND mtime within stale window).
# Finished embed_full is never live.
is_live_job() {
  _path="$1"
  _resolved=$(resolve_log "$_path")
  _pid="${2:-}"
  if is_full_name "$_path" || is_full_name "$_resolved"; then
    if log_is_done "$_resolved"; then
      return 1
    fi
    if pid_alive "$_pid"; then
      return 0
    fi
    # No pid and done-or-old full run: not live.
    _age=$(log_age "$_path")
    if [ "$_age" -gt "$STALE_SEC" ]; then
      return 1
    fi
    return 0
  fi
  if pid_alive "$_pid"; then
    return 0
  fi
  if log_is_done "$_resolved"; then
    return 1
  fi
  return 0
}

find_pid_for_log() {
  _want=$(resolve_log "$1")
  for _dir in "$RUN_DIR" "$LOGS_DIR"; do
    [ -d "$_dir" ] || continue
    set +e
    for _pf in "$_dir"/*.pid; do
      [ -f "$_pf" ] || continue
      _pid=$(head -n 1 "$_pf" | tr -cd '0-9')
      _log=$(sed -n '2p' "$_pf" | tr -d '\r')
      if [ -n "$_log" ]; then
        _got=$(resolve_log "$_log")
        if [ "$_got" = "$_want" ] || [ "$_log" = "$1" ]; then
          echo "$_pid"
          set -e
          return 0
        fi
      fi
    done
    set -e
  done
  return 0
}

pick_jobs() {
  collect_latest_symlinks
  collect_pid_files
  collect_launchagents
  collect_shard_logs
  collect_full_logs

  LIVE_PREF=""
  LIVE_FULL=""
  DONE_FULL=""
  OTHER_LIVE=""
  SEEN_RESOLVED="|"

  # Walk without a pipeline subshell so we can collect strings.
  _old_ifs="$IFS"
  IFS='
'
  for _p in $CAND_PATHS; do
    IFS="$_old_ifs"
    [ -n "$_p" ] || continue
    _res=$(resolve_log "$_p")
    case "$SEEN_RESOLVED" in
      *"|$_res|"*) continue ;;
    esac
    SEEN_RESOLVED="${SEEN_RESOLVED}${_res}|"
    _pid=$(find_pid_for_log "$_p" || true)
    if is_live_job "$_p" "$_pid"; then
      if is_preferred_live_name "$_p"; then
        LIVE_PREF="${LIVE_PREF}${_p}
"
      elif is_full_name "$_p"; then
        LIVE_FULL="${LIVE_FULL}${_p}
"
      else
        OTHER_LIVE="${OTHER_LIVE}${_p}
"
      fi
    else
      if is_full_name "$_p"; then
        DONE_FULL="${DONE_FULL}${_p}
"
      fi
    fi
  done
  IFS="$_old_ifs"

  CHOSEN=""
  IGNORED_FULL="$DONE_FULL"
  if [ -n "$LIVE_PREF" ]; then
    CHOSEN="$LIVE_PREF"
    # A finished (or idle) embed_full must not steal the sample.
    IGNORED_FULL="${IGNORED_FULL}${LIVE_FULL}"
  elif [ -n "$LIVE_FULL" ]; then
    CHOSEN="$LIVE_FULL"
  elif [ -n "$OTHER_LIVE" ]; then
    CHOSEN="$OTHER_LIVE"
  fi
}

json_escape() {
  printf '%s' "$1" | awk '
    BEGIN { ORS="" }
    {
      gsub(/\\/, "\\\\")
      gsub(/"/, "\\\"")
      gsub(/\t/, "\\t")
      printf "%s", $0
    }
  '
}

print_sample() {
  _now=$(now_ts)
  _jobs_json=""
  _any_stall=0
  _any_job=0
  _first=1

  if [ "$MODE" != "pick" ]; then
    echo "embed_health_sample  logs=$LOGS_DIR  stale_sec=$STALE_SEC"
  fi

  if [ "$MODE" != "pick" ] && [ -n "$IGNORED_FULL" ]; then
    echo "$IGNORED_FULL" | while IFS= read -r _p; do
      [ -n "$_p" ] || continue
      echo "ignored: $_p (finished embed_full; not a live rem/shard job)"
    done
  fi

  if [ -z "$CHOSEN" ]; then
    echo "status: idle  (no live rem1/rem3/.latest/shard job)"
    if [ "$MODE" = "json" ]; then
      printf '{"ok":true,"status":"idle","soft_stall":false,"jobs":[],"ignored_embed_full":[]}\n'
    fi
    return 0
  fi

  _old_ifs="$IFS"
  IFS='
'
  for _p in $CHOSEN; do
    IFS="$_old_ifs"
    [ -n "$_p" ] || continue
    _any_job=1
    _resolved=$(resolve_log "$_p")
    _kind=$(job_kind "$_p")
    _pid=$(find_pid_for_log "$_p" || true)
    _alive=0
    if pid_alive "$_pid"; then
      _alive=1
    fi
    _age=$(log_age "$_p")
    _pair=$(read_committed "$_resolved" || true)
    _c=""
    _t=""
    if [ -n "$_pair" ]; then
      _c=${_pair%% *}
      _t=${_pair##* }
    fi
    _done=0
    if log_is_done "$_resolved"; then
      _done=1
    fi
    _stall=0
    # Soft-stall only for a live (not-done) preferred job whose log is quiet.
    if [ "$_done" -eq 0 ] && [ "$_age" -gt "$STALE_SEC" ]; then
      if [ "$_alive" -eq 1 ] || is_preferred_live_name "$_p"; then
        _stall=1
        _any_stall=1
      fi
    fi
    _status="live"
    [ "$_done" -eq 1 ] && _status="done"
    [ "$_stall" -eq 1 ] && _status="soft-stall"

    if [ "$MODE" = "pick" ]; then
      echo "$_p"
    else
      echo "job: $_kind"
      echo "  log: $_p"
      if [ "$_resolved" != "$_p" ]; then
        echo "  target: $_resolved"
      fi
      if [ -n "$_pid" ]; then
        if [ "$_alive" -eq 1 ]; then
          echo "  pid: $_pid (live)"
        else
          echo "  pid: $_pid (dead)"
        fi
      else
        echo "  pid: -"
      fi
      if [ -n "$_c" ]; then
        echo "  committed: ${_c}/${_t}"
      else
        echo "  committed: -"
      fi
      echo "  age_sec: $_age"
      echo "  status: $_status"
    fi

    if [ "$MODE" = "json" ]; then
      [ "$_first" -eq 1 ] || _jobs_json="${_jobs_json},"
      _first=0
      _jobs_json="${_jobs_json}{\"kind\":\"$(json_escape "$_kind")\",\"log\":\"$(json_escape "$_p")\",\"target\":\"$(json_escape "$_resolved")\",\"pid\":\"$(json_escape "${_pid:-}")\",\"pid_live\":$([ "$_alive" -eq 1 ] && echo true || echo false),\"committed\":${_c:-null},\"total\":${_t:-null},\"age_sec\":$_age,\"done\":$([ "$_done" -eq 1 ] && echo true || echo false),\"soft_stall\":$([ "$_stall" -eq 1 ] && echo true || echo false),\"status\":\"$_status\"}"
    fi
  done
  IFS="$_old_ifs"

  if [ "$MODE" = "json" ]; then
    _ign="["
    _if=1
    _old_ifs="$IFS"
    IFS='
'
    for _p in $IGNORED_FULL; do
      IFS="$_old_ifs"
      [ -n "$_p" ] || continue
      [ "$_if" -eq 1 ] || _ign="${_ign},"
      _if=0
      _ign="${_ign}\"$(json_escape "$_p")\""
    done
    IFS="$_old_ifs"
    _ign="${_ign}]"
    _st="live"
    [ "$_any_stall" -eq 1 ] && _st="soft-stall"
    printf '{"ok":true,"status":"%s","soft_stall":%s,"jobs":[%s],"ignored_embed_full":%s}\n' \
      "$_st" \
      "$([ "$_any_stall" -eq 1 ] && echo true || echo false)" \
      "$_jobs_json" \
      "$_ign"
  fi

  if [ "$_any_stall" -eq 1 ]; then
    return 1
  fi
  return 0
}

pick_jobs
print_sample
exit $?
