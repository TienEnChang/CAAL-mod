#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="/Users/altspace/Public/caal"
LABEL="com.coreworxlab.caal"
SOURCE_PLIST="$PROJECT_DIR/launchd/$LABEL.plist"
TARGET_PLIST="/Library/LaunchDaemons/$LABEL.plist"
ACTION="${1:-install}"

usage() {
  cat <<EOF
Usage: ./launchd/manage.sh <install|start|stop|restart|status|logs|uninstall>
EOF
}

if [[ "$ACTION" == "status" ]]; then
  exec launchctl print "system/$LABEL"
fi

if [[ "$ACTION" == "logs" ]]; then
  touch "$PROJECT_DIR/.native/logs/launchd.log" "$PROJECT_DIR/.native/logs/launchd.error.log"
  exec tail -n 80 -f \
    "$PROJECT_DIR/.native/logs/launchd.log" \
    "$PROJECT_DIR/.native/logs/launchd.error.log"
fi

case "$ACTION" in
  install|start|stop|restart|uninstall) ;;
  *) usage >&2; exit 2 ;;
esac

if (( EUID != 0 )); then
  exec sudo "$0" "$ACTION"
fi

case "$ACTION" in
  install)
    launchctl bootout "system/$LABEL" 2>/dev/null || true
    install -o root -g wheel -m 0644 "$SOURCE_PLIST" "$TARGET_PLIST"
    launchctl bootstrap system "$TARGET_PLIST"
    launchctl enable "system/$LABEL"
    echo "CAAL LaunchDaemon installed and started."
    ;;
  start)
    [[ -f "$TARGET_PLIST" ]] || {
      echo "CAAL LaunchDaemon is not installed; run: ./launchd/manage.sh install" >&2
      exit 1
    }
    if launchctl print "system/$LABEL" >/dev/null 2>&1; then
      launchctl kickstart "system/$LABEL"
    else
      launchctl bootstrap system "$TARGET_PLIST"
    fi
    echo "CAAL LaunchDaemon started."
    ;;
  stop)
    launchctl bootout "system/$LABEL" 2>/dev/null || true
    echo "CAAL LaunchDaemon stopped."
    ;;
  restart)
    [[ -f "$TARGET_PLIST" ]] || {
      echo "CAAL LaunchDaemon is not installed; run: ./launchd/manage.sh install" >&2
      exit 1
    }
    if launchctl print "system/$LABEL" >/dev/null 2>&1; then
      launchctl kickstart -k "system/$LABEL"
    else
      launchctl bootstrap system "$TARGET_PLIST"
    fi
    echo "CAAL LaunchDaemon restarted."
    ;;
  uninstall)
    launchctl bootout "system/$LABEL" 2>/dev/null || true
    rm -f "$TARGET_PLIST"
    echo "CAAL LaunchDaemon stopped and removed."
    ;;
esac
