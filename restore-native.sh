#!/usr/bin/env bash
# Restore native CAAL state from one encrypted backup.

set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -lt 1 ]]; then
  echo "Usage: ./restore-native.sh BACKUP.caalbak [--force] [--prompt|--password-file FILE]" >&2
  exit 2
fi

PYTHON="${CAAL_BACKUP_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON. Install the project dependencies before restoring." >&2
  exit 1
fi

SOURCE="$1"
shift
exec "$PYTHON" scripts/native_state.py --project "$PWD" restore "$SOURCE" "$@"
