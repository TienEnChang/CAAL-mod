#!/usr/bin/env bash

set -uo pipefail

PROJECT_DIR="/Users/altspace/Public/caal"
child_pid=""
monitor_pid=""

stop_monitor() {
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill -TERM "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
  monitor_pid=""
}

shutdown() {
  trap - INT TERM
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  stop_monitor
  exit 0
}

trap shutdown INT TERM
cd "$PROJECT_DIR"
export CAAL_LAUNCHD=true

"$PROJECT_DIR/start-native.sh" --tmux-child &
child_pid=$!
if [[ "${CAAL_MEMORY_MONITOR_ENABLED:-true}" != "false" ]]; then
  "$PROJECT_DIR/.venv/bin/python" \
    "$PROJECT_DIR/scripts/native_memory_monitor.py" \
    --project "$PROJECT_DIR" monitor \
    --sample-seconds "${CAAL_MEMORY_SAMPLE_SECONDS:-60}" \
    --deep-seconds "${CAAL_MEMORY_DEEP_SAMPLE_SECONDS:-300}" \
    --swap-restart-gb "${CAAL_SWAP_RESTART_GB:-3}" \
    >>"$PROJECT_DIR/.native/logs/memory-monitor.log" 2>&1 &
  monitor_pid=$!
fi
wait "$child_pid"
status=$?
stop_monitor
exit "$status"
