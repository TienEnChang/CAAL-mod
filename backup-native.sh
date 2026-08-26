#!/usr/bin/env bash
# Create one minimal encrypted backup of native CAAL state.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${CAAL_BACKUP_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON. Install the project dependencies before backing up." >&2
  exit 1
fi

OUTPUT="${1:-caal-native-$(date +%Y%m%d-%H%M%S).caalbak}"
shift $(( $# > 0 ? 1 : 0 ))
exec "$PYTHON" scripts/native_state.py --project "$PWD" backup "$OUTPUT" "$@"
