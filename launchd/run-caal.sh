#!/usr/bin/env bash

set -uo pipefail

PROJECT_DIR="/Users/altspace/Public/caal"
child_pid=""

shutdown() {
  trap - INT TERM
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 0
}

trap shutdown INT TERM
cd "$PROJECT_DIR"
export CAAL_LAUNCHD=true

"$PROJECT_DIR/start-native.sh" --tmux-child &
child_pid=$!
wait "$child_pid"
exit $?
