#!/usr/bin/env bash
# Run CAAL entirely on macOS: no Docker and no cloud inference.
#
# Usage:
#   ./start-native.sh                       Start and supervise the full stack
#   ./start-native.sh --models              Run Qwen + speech services only
#   ./start-native.sh --app                 Run app services; reuse healthy models
#   ./start-native.sh --restart <service>   Restart one service in place
#   ./start-native.sh --stop                Stop all native services
#   ./start-native.sh --status              Show service status
#   ./start-native.sh --logs [service]      Follow one service log (agent by default)
#   ./start-native.sh --help                Show commands and port overrides

set -euo pipefail
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
RUNTIME_DIR="$PROJECT_DIR/.native"
BIN_DIR="$RUNTIME_DIR/bin"
CONFIG_DIR="$RUNTIME_DIR/config"
DATA_DIR="$RUNTIME_DIR/data"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"
SUPERVISOR_DIR="$RUNTIME_DIR/supervisors"

PROTOVOICE_DIR="${CAAL_PROTOVOICE_DIR:-$(dirname "$PROJECT_DIR")/protoVoice}"
MODEL_PYTHON="${CAAL_MLX_PYTHON:-$PROTOVOICE_DIR/.venv/bin/python}"
MLX_SPEECH_VENV="$RUNTIME_DIR/mlx-speech-venv"
SPEECH_PYTHON="${CAAL_MLX_SPEECH_PYTHON:-$MLX_SPEECH_VENV/bin/python}"
UV_BIN="${CAAL_UV_BIN:-$(command -v uv || true)}"
AGENT_PYTHON="$PROJECT_DIR/.venv/bin/python"
LIVEKIT_BIN="$BIN_DIR/livekit-server"
NODE_BIN="${CAAL_NODE_BIN:-/Users/altspace/.local/node/bin/node}"
NEXT_STANDALONE="$PROJECT_DIR/frontend/.next/standalone"
NEXT_SERVER="$NEXT_STANDALONE/server.js"

QWEN_MODEL="${CAAL_QWEN_MODEL:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
QWEN_PORT="${CAAL_QWEN_PORT:-8100}"
SPEECH_PORT="${CAAL_SPEECH_PORT:-8001}"
LIVEKIT_PORT="${CAAL_LIVEKIT_PORT:-7880}"
LIVEKIT_RTC_TCP_PORT="${CAAL_LIVEKIT_RTC_TCP_PORT:-7881}"
WEBHOOK_PORT="${CAAL_WEBHOOK_PORT:-8889}"
FRONTEND_PORT="${CAAL_FRONTEND_PORT:-3000}"

ALL_SERVICES=(qwen speech livekit agent frontend)
MODEL_SERVICES=(qwen speech)
APP_SERVICES=(livekit agent frontend)

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$PID_DIR" "$SUPERVISOR_DIR"

usage() {
  cat <<EOF
CAAL native Apple Silicon launcher

Commands:
  (no arguments)              Start and supervise every service
  --models                    Start and supervise Qwen, Whisper, and Kokoro
  --app                       Start LiveKit, agent, and frontend using healthy models
  --restart <service>         Restart qwen, speech, livekit, agent, or frontend
  --stop                      Stop every native service
  --status                    Show service PIDs and detect untracked CAAL processes
  --logs [service]            Follow a service log; defaults to agent
  --help                      Show this help

Port overrides:
  CAAL_QWEN_PORT              Qwen OpenAI-compatible API ($QWEN_PORT)
  CAAL_SPEECH_PORT            Whisper/Kokoro bridge ($SPEECH_PORT)
  CAAL_MLX_SPEECH_PYTHON      Existing Python environment with MLX speech packages
  CAAL_LIVEKIT_PORT           LiveKit WebSocket/API ($LIVEKIT_PORT)
  CAAL_LIVEKIT_RTC_TCP_PORT   LiveKit RTC fallback TCP ($LIVEKIT_RTC_TCP_PORT)
  CAAL_WEBHOOK_PORT           Agent webhook API ($WEBHOOK_PORT)
  CAAL_FRONTEND_PORT          CAAL web interface ($FRONTEND_PORT)
EOF
}

is_service() {
  case "$1" in
    qwen|speech|livekit|agent|frontend) return 0 ;;
    *) return 1 ;;
  esac
}

supervisor_is_running() {
  local mode="$1" file="$SUPERVISOR_DIR/$1.pid" pid
  [[ -f "$file" ]] || return 1
  pid="$(<"$file")"
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q '[s]tart-native.sh'
}

register_supervisor() {
  echo "$$" >"$SUPERVISOR_DIR/$1.pid"
}

unregister_supervisor() {
  local file="$SUPERVISOR_DIR/$1.pid"
  [[ -f "$file" ]] && [[ "$(<"$file")" == "$$" ]] && rm -f "$file"
  return 0
}

stop_supervisor() {
  local mode="$1" file="$SUPERVISOR_DIR/$1.pid" pid
  [[ -f "$file" ]] || return 0
  pid="$(<"$file")"
  if supervisor_is_running "$mode"; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
}

stop_supervisors() {
  local mode
  for mode in "$@"; do stop_supervisor "$mode"; done
}

service_port() {
  case "$1" in
    qwen) echo "$QWEN_PORT" ;;
    speech) echo "$SPEECH_PORT" ;;
    livekit) echo "$LIVEKIT_PORT" ;;
    agent) echo "$WEBHOOK_PORT" ;;
    frontend) echo "$FRONTEND_PORT" ;;
  esac
}

service_url() {
  case "$1" in
    qwen) echo "http://127.0.0.1:$QWEN_PORT/v1/models" ;;
    speech) echo "http://127.0.0.1:$SPEECH_PORT/health" ;;
    livekit) echo "http://127.0.0.1:$LIVEKIT_PORT" ;;
    agent) echo "http://127.0.0.1:$WEBHOOK_PORT/health" ;;
    frontend) echo "http://127.0.0.1:$FRONTEND_PORT" ;;
  esac
}

validate_port() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > 65535 )); then
    echo "Invalid $name port: $value" >&2
    exit 1
  fi
}

pid_matches_service() {
  local name="$1" pid="$2" command
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$name:$command" in
    qwen:*mlx_lm*server*) return 0 ;;
    speech:*local_speech_server.py*) return 0 ;;
    livekit:*livekit-server*) return 0 ;;
    agent:*voice_agent.py*start*) return 0 ;;
    frontend:*standalone/server.js*|frontend:*next-server*) return 0 ;;
    *) return 1 ;;
  esac
}

matching_pids_on_port() {
  local name="$1" port candidates pid
  port="$(service_port "$name")"
  candidates="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "$candidates" ]] || return 0
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pid_matches_service "$name" "$pid" && echo "$pid"
  done <<<"$candidates"
  return 0
}

stop_pid() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
}

stop_one() {
  local name="$1" file="$PID_DIR/$1.pid" pid recovered=""
  if [[ -f "$file" ]]; then
    pid="$(<"$file")"
    if pid_matches_service "$name" "$pid"; then
      stop_pid "$pid"
    elif kill -0 "$pid" 2>/dev/null; then
      echo "Ignored stale $name PID file: PID $pid belongs to another process" >&2
    fi
    rm -f "$file"
  fi

  recovered="$(matching_pids_on_port "$name")"
  if [[ -n "$recovered" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && stop_pid "$pid"
    done <<<"$recovered"
    echo "Stopped $name (recovered untracked process)"
  else
    echo "Stopped $name"
  fi
}

stop_services() {
  local name
  for name in "$@"; do stop_one "$name"; done
}

stop_all() {
  stop_services frontend agent livekit speech qwen
}

status_one() {
  local name="$1" file="$PID_DIR/$1.pid" pid recovered
  if [[ -f "$file" ]]; then
    pid="$(<"$file")"
    if pid_matches_service "$name" "$pid"; then
      echo "$name: running (PID $pid, port $(service_port "$name"))"
      return
    fi
  fi
  recovered="$(matching_pids_on_port "$name")"
  if [[ -n "$recovered" ]]; then
    echo "$name: running untracked (PID ${recovered//$'\n'/, }, port $(service_port "$name"))"
  else
    echo "$name: stopped"
  fi
}

status_supervisors() {
  local active=() mode
  for mode in all models app; do
    supervisor_is_running "$mode" && active+=("$mode")
  done
  if (( ${#active[@]} > 0 )); then
    echo "supervisor: ${active[*]}"
  else
    echo "supervisor: none"
  fi
}

validate_service_dependency() {
  local name="$1" executable
  case "$name" in
    qwen) executable="$MODEL_PYTHON" ;;
    speech)
      ensure_mlx_speech_environment
      executable="$SPEECH_PYTHON"
      ;;
    livekit) executable="$LIVEKIT_BIN" ;;
    agent) executable="$AGENT_PYTHON" ;;
    frontend) executable="$NODE_BIN" ;;
  esac
  [[ -x "$executable" ]] || {
    echo "Missing native dependency for $name: $executable" >&2
    return 1
  }
  if [[ "$name" == "frontend" && ! -f "$NEXT_SERVER" ]]; then
    echo "Missing native frontend build: $NEXT_SERVER" >&2
    return 1
  fi
}

ensure_mlx_speech_environment() {
  if [[ -x "$SPEECH_PYTHON" ]] && "$SPEECH_PYTHON" -c \
    'import mlx_audio, mlx_whisper, soxr' >/dev/null 2>&1; then
    return 0
  fi
  [[ -n "$UV_BIN" && -x "$UV_BIN" ]] || {
    echo "Missing uv; install it or set CAAL_MLX_SPEECH_PYTHON" >&2
    return 1
  }
  [[ -x "$MODEL_PYTHON" ]] || {
    echo "Missing Python used to create the MLX speech environment: $MODEL_PYTHON" >&2
    return 1
  }
  echo "Creating dedicated MLX speech environment..."
  "$UV_BIN" venv --python "$MODEL_PYTHON" "$MLX_SPEECH_VENV"
  "$UV_BIN" pip install --python "$SPEECH_PYTHON" \
    -r "$PROJECT_DIR/requirements-mlx-speech.txt"
}

prepare_frontend() {
  [[ -e "$NEXT_STANDALONE/public" ]] || ln -s ../../public "$NEXT_STANDALONE/public"
  mkdir -p "$NEXT_STANDALONE/.next"
  [[ -e "$NEXT_STANDALONE/.next/static" ]] || \
    ln -s ../../static "$NEXT_STANDALONE/.next/static"
}

start_service() {
  local name="$1"
  shift
  nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  echo "  started $name (PID $pid)"
}

start_service_in() {
  local directory="$1" name="$2"
  shift 2
  (
    cd "$directory"
    exec nohup "$@"
  ) >"$LOG_DIR/$name.log" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  echo "  started $name (PID $pid)"
}

wait_http() {
  local name="$1" url pid_file="$PID_DIR/$1.pid"
  url="$(service_url "$name")"
  for _ in $(seq 1 120); do
    if [[ -f "$pid_file" ]] && ! kill -0 "$(<"$pid_file")" 2>/dev/null; then
      echo "$name exited during startup:" >&2
      tail -40 "$LOG_DIR/$name.log" >&2
      return 1
    fi
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "$name did not become ready at $url; see $LOG_DIR/$name.log" >&2
  return 1
}

start_named() {
  local name="$1"
  validate_service_dependency "$name"
  case "$name" in
    qwen)
      echo "Starting Qwen: $QWEN_MODEL → http://127.0.0.1:$QWEN_PORT/v1"
      start_service qwen "$MODEL_PYTHON" -m mlx_lm server \
        --model "$QWEN_MODEL" --host 127.0.0.1 --port "$QWEN_PORT"
      ;;
    speech)
      echo "Starting MLX Whisper + MLX Kokoro → http://127.0.0.1:$SPEECH_PORT"
      start_service speech env \
        SPEECH_HOST="127.0.0.1" SPEECH_PORT="$SPEECH_PORT" \
        "$SPEECH_PYTHON" "$PROJECT_DIR/local_speech_server.py"
      ;;
    livekit)
      echo "Starting LiveKit → ws://127.0.0.1:$LIVEKIT_PORT"
      start_service livekit "$LIVEKIT_BIN" --dev --bind 127.0.0.1 \
        --port "$LIVEKIT_PORT" --rtc.tcp_port "$LIVEKIT_RTC_TCP_PORT"
      ;;
    agent)
      echo "Starting CAAL agent → http://127.0.0.1:$WEBHOOK_PORT"
      start_service agent "$AGENT_PYTHON" "$PROJECT_DIR/voice_agent.py" start
      ;;
    frontend)
      prepare_frontend
      echo "Starting CAAL frontend → http://localhost:$FRONTEND_PORT"
      start_service_in "$NEXT_STANDALONE" frontend env \
        LIVEKIT_URL="ws://127.0.0.1:$LIVEKIT_PORT" \
        LIVEKIT_API_KEY="devkey" \
        LIVEKIT_API_SECRET="secret" \
        NEXT_PUBLIC_LIVEKIT_URL="auto" \
        LIVEKIT_PUBLIC_PORT="$LIVEKIT_PORT" \
        WEBHOOK_URL="http://127.0.0.1:$WEBHOOK_PORT" \
        HOSTNAME="127.0.0.1" \
        PORT="$FRONTEND_PORT" \
        "$NODE_BIN" "$NEXT_SERVER"
      ;;
  esac
  wait_http "$name"
  if [[ "$name" == "speech" ]]; then
    curl -fsS -X POST \
      "http://127.0.0.1:$SPEECH_PORT/v1/models?model_name=${CAAL_WHISPER_MODEL:-mlx-community/distil-whisper-medium.en}" \
      >/dev/null
    curl -fsS -X POST \
      "http://127.0.0.1:$SPEECH_PORT/v1/models?model_name=${CAAL_KOKORO_MODEL:-mlx-community/Kokoro-82M-bf16}" \
      >/dev/null
  fi
}

start_services() {
  local name
  for name in "$@"; do start_named "$name"; done
}

require_healthy_models() {
  local name
  for name in "${MODEL_SERVICES[@]}"; do
    if ! curl -fsS "$(service_url "$name")" >/dev/null 2>&1; then
      echo "$name is not healthy; run ./start-native.sh --models first" >&2
      return 1
    fi
  done
}

monitor_services() {
  local managed=("$@") name healthy attempt
  while true; do
    for name in "${managed[@]}"; do
      healthy=false
      # A targeted restart briefly replaces its PID file. Allow enough time
      # for shells under load without masking a real failure indefinitely.
      for attempt in $(seq 1 20); do
        if [[ -f "$PID_DIR/$name.pid" ]] && \
          pid_matches_service "$name" "$(<"$PID_DIR/$name.pid")"; then
          healthy=true
          break
        fi
        sleep 1
      done
      if [[ "$healthy" != true ]]; then
        echo "$name stopped unexpectedly; see $LOG_DIR/$name.log" >&2
        return 1
      fi
    done
    sleep 2
  done
}

cleanup_all() {
  trap - EXIT
  stop_all >/dev/null 2>&1 || true
  unregister_supervisor all
}

cleanup_models() {
  trap - EXIT
  stop_services speech qwen >/dev/null 2>&1 || true
  unregister_supervisor models
}

cleanup_app() {
  trap - EXIT
  stop_services frontend agent livekit >/dev/null 2>&1 || true
  unregister_supervisor app
}

validate_port CAAL_QWEN_PORT "$QWEN_PORT"
validate_port CAAL_SPEECH_PORT "$SPEECH_PORT"
validate_port CAAL_LIVEKIT_PORT "$LIVEKIT_PORT"
validate_port CAAL_LIVEKIT_RTC_TCP_PORT "$LIVEKIT_RTC_TCP_PORT"
validate_port CAAL_WEBHOOK_PORT "$WEBHOOK_PORT"
validate_port CAAL_FRONTEND_PORT "$FRONTEND_PORT"

[[ -f "$CONFIG_DIR/settings.json" ]] || cp settings.native.default.json "$CONFIG_DIR/settings.json"
[[ -f mcp_servers.json ]] || cp mcp_servers.default.json mcp_servers.json

export LIVEKIT_URL="ws://127.0.0.1:$LIVEKIT_PORT"
export LIVEKIT_API_KEY="devkey"
export LIVEKIT_API_SECRET="secret"
export WEBHOOK_HOST="127.0.0.1"
export WEBHOOK_PORT
export CAAL_SETTINGS_PATH="$CONFIG_DIR/settings.json"
export CAAL_REGISTRY_CACHE_PATH="$CONFIG_DIR/registry_cache.json"
export CAAL_MEMORY_DIR="$DATA_DIR"
export CAAL_PROMPT_DIR="$PROJECT_DIR/prompt"
export LLM_PROVIDER="openai_compatible"
export OPENAI_API_KEY="not-needed"
export OPENAI_BASE_URL="http://127.0.0.1:$QWEN_PORT/v1"
export OPENAI_MODEL="$QWEN_MODEL"
export STT_PROVIDER="speaches"
export SPEACHES_URL="http://127.0.0.1:$SPEECH_PORT"
export WHISPER_MODEL="${CAAL_WHISPER_MODEL:-mlx-community/distil-whisper-medium.en}"
export TTS_PROVIDER="kokoro"
export KOKORO_URL="http://127.0.0.1:$SPEECH_PORT"
export TTS_MODEL="${CAAL_KOKORO_MODEL:-mlx-community/Kokoro-82M-bf16}"
export TTS_VOICE="${CAAL_KOKORO_VOICE:-af_heart}"
export TIMEZONE="${CAAL_TIMEZONE:-Asia/Taipei}"
export TIMEZONE_DISPLAY="${CAAL_TIMEZONE_DISPLAY:-Taipei Time}"

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --stop)
    stop_supervisors all app models
    stop_all
    exit 0
    ;;
  --status)
    for name in "${ALL_SERVICES[@]}"; do status_one "$name"; done
    status_supervisors
    exit 0
    ;;
  --logs)
    log_service="${2:-agent}"
    is_service "$log_service" || {
      echo "Unknown service: $log_service" >&2
      usage >&2
      exit 2
    }
    touch "$LOG_DIR/$log_service.log"
    exec tail -n 80 -f "$LOG_DIR/$log_service.log"
    ;;
  --restart)
    restart_service="${2:-}"
    is_service "$restart_service" || {
      echo "Usage: ./start-native.sh --restart <qwen|speech|livekit|agent|frontend>" >&2
      exit 2
    }
    stop_one "$restart_service"
    start_named "$restart_service"
    echo "$restart_service restarted successfully"
    exit 0
    ;;
  --models)
    if supervisor_is_running all; then
      echo "The full-stack supervisor is running; stop it before using --models." >&2
      exit 1
    fi
    stop_supervisor models
    stop_services speech qwen
    register_supervisor models
    trap cleanup_models EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    start_services "${MODEL_SERVICES[@]}"
    echo "Model services are ready. Press Ctrl-C to stop them."
    monitor_services "${MODEL_SERVICES[@]}"
    ;;
  --app)
    if supervisor_is_running all; then
      echo "The full-stack supervisor is running; stop it before using --app." >&2
      exit 1
    fi
    require_healthy_models
    stop_supervisor app
    stop_services frontend agent livekit
    register_supervisor app
    trap cleanup_app EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    start_services "${APP_SERVICES[@]}"
    echo "CAAL is ready: http://localhost:$FRONTEND_PORT"
    echo "Press Ctrl-C to stop app services; model services will stay loaded."
    monitor_services "${APP_SERVICES[@]}"
    ;;
  "") ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac

stop_supervisors all app models
stop_all >/dev/null 2>&1 || true
register_supervisor all
trap cleanup_all EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_services "${ALL_SERVICES[@]}"

echo
echo "CAAL is ready: http://localhost:$FRONTEND_PORT"
echo "Qwen: $QWEN_MODEL"
echo "Whisper: $WHISPER_MODEL"
echo "Kokoro: $TTS_MODEL ($TTS_VOICE)"
echo "Press Ctrl-C to stop all native services."

monitor_services "${ALL_SERVICES[@]}"
