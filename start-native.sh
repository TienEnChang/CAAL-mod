#!/usr/bin/env bash
# Run CAAL entirely on macOS: no Docker and no cloud inference.

set -euo pipefail
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
RUNTIME_DIR="$PROJECT_DIR/.native"
BIN_DIR="$RUNTIME_DIR/bin"
CONFIG_DIR="$RUNTIME_DIR/config"
DATA_DIR="$RUNTIME_DIR/data"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"

PROTOVOICE_DIR="${CAAL_PROTOVOICE_DIR:-$(dirname "$PROJECT_DIR")/protoVoice}"
MODEL_PYTHON="${CAAL_MLX_PYTHON:-$PROTOVOICE_DIR/.venv/bin/python}"
AGENT_PYTHON="$PROJECT_DIR/.venv/bin/python"
LIVEKIT_BIN="$BIN_DIR/livekit-server"
NODE_BIN="${CAAL_NODE_BIN:-/Users/altspace/.local/node/bin/node}"
NEXT_STANDALONE="$PROJECT_DIR/frontend/.next/standalone"
NEXT_SERVER="$NEXT_STANDALONE/server.js"

QWEN_MODEL="${CAAL_QWEN_MODEL:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$PID_DIR"

stop_one() {
  local name="$1" file="$PID_DIR/$1.pid" pid
  [[ -f "$file" ]] || return 0
  pid="$(<"$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
  echo "Stopped $name"
}

stop_all() {
  stop_one frontend
  stop_one agent
  stop_one livekit
  stop_one speech
  stop_one qwen
}

status_one() {
  local name="$1" file="$PID_DIR/$1.pid" pid
  if [[ -f "$file" ]]; then
    pid="$(<"$file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "$name: running (PID $pid)"
      return
    fi
  fi
  echo "$name: stopped"
}

case "${1:-}" in
  --stop) stop_all; exit 0 ;;
  --status)
    status_one qwen
    status_one speech
    status_one livekit
    status_one agent
    status_one frontend
    exit 0
    ;;
esac

for executable in "$MODEL_PYTHON" "$AGENT_PYTHON" "$LIVEKIT_BIN" "$NODE_BIN"; do
  [[ -x "$executable" ]] || {
    echo "Missing native dependency: $executable" >&2
    exit 1
  }
done
[[ -f "$NEXT_SERVER" ]] || {
  echo "Missing native frontend build: $NEXT_SERVER" >&2
  exit 1
}

# Next's standalone build omits static assets; expose the source copies without
# duplicating them. The links remain inside the ignored build directory.
[[ -e "$NEXT_STANDALONE/public" ]] || ln -s ../../public "$NEXT_STANDALONE/public"
mkdir -p "$NEXT_STANDALONE/.next"
[[ -e "$NEXT_STANDALONE/.next/static" ]] || \
  ln -s ../../static "$NEXT_STANDALONE/.next/static"

[[ -f "$CONFIG_DIR/settings.json" ]] || cp settings.native.default.json "$CONFIG_DIR/settings.json"
[[ -f mcp_servers.json ]] || cp mcp_servers.default.json mcp_servers.json

export LIVEKIT_URL="ws://127.0.0.1:7880"
export LIVEKIT_API_KEY="devkey"
export LIVEKIT_API_SECRET="secret"
export WEBHOOK_HOST="127.0.0.1"
export WEBHOOK_PORT="8889"
export CAAL_SETTINGS_PATH="$CONFIG_DIR/settings.json"
export CAAL_REGISTRY_CACHE_PATH="$CONFIG_DIR/registry_cache.json"
export CAAL_MEMORY_DIR="$DATA_DIR"
export CAAL_PROMPT_DIR="$PROJECT_DIR/prompt"
export LLM_PROVIDER="openai_compatible"
export OPENAI_API_KEY="not-needed"
export OPENAI_BASE_URL="http://127.0.0.1:8100/v1"
export OPENAI_MODEL="$QWEN_MODEL"
export STT_PROVIDER="speaches"
export SPEACHES_URL="http://127.0.0.1:8001"
export WHISPER_MODEL="${CAAL_WHISPER_MODEL:-distil-whisper/distil-medium.en}"
export TTS_PROVIDER="kokoro"
export KOKORO_URL="http://127.0.0.1:8001"
export TTS_MODEL="${CAAL_KOKORO_MODEL:-hexgrad/Kokoro-82M}"
export TTS_VOICE="${CAAL_KOKORO_VOICE:-af_heart}"
export TIMEZONE="Asia/Taipei"
export TIMEZONE_DISPLAY="Taipei Time"

start_service() {
  local name="$1"
  shift
  "$@" >"$LOG_DIR/$name.log" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  echo "Started $name (PID $pid)"
}

start_service_in() {
  local directory="$1" name="$2"
  shift 2
  (
    cd "$directory"
    exec "$@"
  ) >"$LOG_DIR/$name.log" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  echo "Started $name (PID $pid)"
}

wait_http() {
  local name="$1" url="$2" pid_file="$PID_DIR/$1.pid"
  for _ in $(seq 1 120); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    if [[ -f "$pid_file" ]] && ! kill -0 "$(<"$pid_file")" 2>/dev/null; then
      echo "$name exited during startup:" >&2
      tail -40 "$LOG_DIR/$name.log" >&2
      return 1
    fi
    sleep 1
  done
  echo "$name did not become ready; see $LOG_DIR/$name.log" >&2
  return 1
}

stop_all >/dev/null 2>&1 || true
trap 'stop_all >/dev/null 2>&1 || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_service qwen "$MODEL_PYTHON" -m mlx_lm server \
  --model "$QWEN_MODEL" --host 127.0.0.1 --port 8100
wait_http qwen http://127.0.0.1:8100/v1/models

start_service speech "$MODEL_PYTHON" "$PROJECT_DIR/local_speech_server.py"
wait_http speech http://127.0.0.1:8001/health
curl -fsS -X POST \
  "http://127.0.0.1:8001/v1/models?model_name=${CAAL_KOKORO_MODEL:-hexgrad/Kokoro-82M}" \
  >/dev/null

start_service livekit "$LIVEKIT_BIN" --dev --bind 127.0.0.1
wait_http livekit http://127.0.0.1:7880

start_service agent "$AGENT_PYTHON" "$PROJECT_DIR/voice_agent.py" start
wait_http agent http://127.0.0.1:8889/health

start_service_in "$NEXT_STANDALONE" frontend env \
  LIVEKIT_URL="ws://127.0.0.1:7880" \
  LIVEKIT_API_KEY="devkey" \
  LIVEKIT_API_SECRET="secret" \
  NEXT_PUBLIC_LIVEKIT_URL="auto" \
  WEBHOOK_URL="http://127.0.0.1:8889" \
  HOSTNAME="127.0.0.1" \
  PORT="3000" \
  "$NODE_BIN" "$NEXT_SERVER"
wait_http frontend http://127.0.0.1:3000

echo
echo "CAAL is ready: http://localhost:3000"
echo "Press Ctrl-C to stop all native services."

while true; do
  for name in qwen speech livekit agent frontend; do
    pid="$(<"$PID_DIR/$name.pid")"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name stopped unexpectedly; see $LOG_DIR/$name.log" >&2
      exit 1
    fi
  done
  sleep 2
done
