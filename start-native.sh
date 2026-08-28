#!/usr/bin/env bash
# Run CAAL entirely on macOS: no Docker and no cloud inference.
#
# Usage:
#   ./start-native.sh                       Start and supervise the full stack
#   ./start-native.sh --models              Run the model + speech services only
#   ./start-native.sh --app                 Run app services; reuse healthy models
#   ./start-native.sh --restart <service>   Restart one service in place
#   ./start-native.sh --install-n8n         Install the bundled n8n runtime
#   ./start-native.sh --stop                Stop all native services
#   ./start-native.sh --status              Show service status
#   ./start-native.sh --memory-report [hrs] Show attributed RAM/Metal/swap growth
#   ./start-native.sh --memory-sample       Record one deep memory snapshot
#   ./start-native.sh --attach [mode]       Attach to the persistent supervisor
#   ./start-native.sh --logs [service]      Follow one service log (agent by default)
#   ./start-native.sh --help                Show commands and port overrides

set -euo pipefail
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
TMUX_BIN="${CAAL_TMUX_BIN:-$(command -v tmux || true)}"
TMUX_SESSION_BASE="${CAAL_TMUX_SESSION:-caal-stable}"
LAUNCHD_LABEL="${CAAL_LAUNCHD_LABEL:-com.coreworxlab.caal}"
TMUX_CHILD=false
if [[ "${1:-}" == "--tmux-child" ]]; then
  TMUX_CHILD=true
  shift
fi

RUNTIME_DIR="$PROJECT_DIR/.native"
BIN_DIR="$RUNTIME_DIR/bin"
CONFIG_DIR="$RUNTIME_DIR/config"
DATA_DIR="$RUNTIME_DIR/data"
LOG_DIR="$RUNTIME_DIR/logs"
# Service logs append across restarts so history survives, and rotate at this
# size keeping one generation - the same convention the memory monitor uses for
# memory.jsonl. Truncating instead would discard the log every restart, and a
# process still holding the old descriptor would write past the new end,
# leaving the file full of NUL padding.
LOG_MAX_BYTES="${CAAL_LOG_MAX_BYTES:-20971520}"
PID_DIR="$RUNTIME_DIR/pids"
SUPERVISOR_DIR="$RUNTIME_DIR/supervisors"

MLX_SPEECH_VENV="$RUNTIME_DIR/mlx-speech-venv"
SPEECH_PYTHON="${CAAL_MLX_SPEECH_PYTHON:-$MLX_SPEECH_VENV/bin/python}"
UV_BIN="${CAAL_UV_BIN:-$(command -v uv || true)}"
UV_PYTHON="${CAAL_UV_PYTHON:-3.12}"
AGENT_PYTHON="$PROJECT_DIR/.venv/bin/python"
LIVEKIT_BIN="$BIN_DIR/livekit-server"
NODE_BIN="${CAAL_NODE_BIN:-/Users/altspace/.local/node/bin/node}"
NEXT_STANDALONE="$PROJECT_DIR/frontend/.next/standalone"
NEXT_SERVER="$NEXT_STANDALONE/server.js"

# n8n needs a newer Node than the frontend runtime, so it gets its own.
N8N_NODE_DIR="$RUNTIME_DIR/node"
N8N_NODE_BIN="${CAAL_N8N_NODE_BIN:-$N8N_NODE_DIR/bin/node}"
N8N_NODE_VERSION="${CAAL_N8N_NODE_VERSION:-v24.19.0}"
N8N_VERSION="${CAAL_N8N_VERSION:-2.36.7}"
N8N_PREFIX="$RUNTIME_DIR/n8n"
N8N_BIN="$N8N_PREFIX/node_modules/n8n/bin/n8n"
N8N_DATA_DIR="$DATA_DIR/n8n"
N8N_KEY_FILE="$CONFIG_DIR/n8n-encryption-key"

# CAAL serves its own model through mlx-lm rather than LM Studio, so that a
# finished call's KV cache can be released without unloading the weights and so
# the wired-memory budget is ours to set. LM Studio may still be installed for
# browsing and downloading models; it is simply not in the serving path.
MLX_MODEL_VENV="$RUNTIME_DIR/mlx-model-venv"
MODEL_PYTHON="${CAAL_MLX_MODEL_PYTHON:-$MLX_MODEL_VENV/bin/python}"
# Any repo id shown by GET /v1/models, which lists the Hugging Face cache.
# The model saved in settings is the source of truth - it is what the agent
# names on every request, and what the web UI writes when someone switches
# models. The server is started on that same model so its built-in default can
# never be a second, competing identity: when the two disagreed, requests that
# named a model got one and requests that did not got the other, and the server
# reloaded back and forth between them on almost every call.
# CAAL_MODEL_ID applies only before anyone has chosen a model.
settings_model() {
  [[ -f "$CONFIG_DIR/settings.json" ]] || return 1
  /usr/bin/python3 -c '
import json, sys
try:
    value = json.load(open(sys.argv[1])).get("openai_model") or ""
except (OSError, ValueError):
    sys.exit(1)
# "caal-model" was the LM Studio instance alias and is not a resolvable repo id.
sys.exit(1) if not value or value == "caal-model" else print(value)
' "$CONFIG_DIR/settings.json" 2>/dev/null
}
MODEL_ID="$(settings_model || true)"
MODEL_ID="${MODEL_ID:-${CAAL_MODEL_ID:-mlx-community/Qwen3-4B-Instruct-2507-4bit}}"
MODEL_PORT="${CAAL_MODEL_PORT:-8100}"
# Held below the 11.84 GiB mlx-lm would otherwise wire on a 16 GiB machine.
# mlx-lm keeps up to ten distinct KV caches by default with no size ceiling.
# Each one holds the whole prompt - system instructions, every tool schema, and
# the history - so a call accumulates roughly 0.33 GiB per assistant turn, and
# nine of those ten are superseded versions of the same conversation that will
# never be reused. Two is what CAAL actually needs: the warmed stable prefix and
# the live conversation branch. The byte ceiling bounds a long call.
MODEL_PROMPT_CACHE_SIZE="${CAAL_MODEL_PROMPT_CACHE_SIZE:-2}"
MODEL_PROMPT_CACHE_BYTES="${CAAL_MODEL_PROMPT_CACHE_BYTES:-2GB}"
MODEL_WIRED_LIMIT_GB="${CAAL_MODEL_WIRED_LIMIT_GB:-6}"
MODEL_MEMORY_LIMIT_GB="${CAAL_MODEL_MEMORY_LIMIT_GB:-10}"
MODEL_PARALLEL="${CAAL_MODEL_PARALLEL:-4}"
SPEECH_PORT="${CAAL_SPEECH_PORT:-8001}"
LIVEKIT_PORT="${CAAL_LIVEKIT_PORT:-7880}"
LIVEKIT_RTC_TCP_PORT="${CAAL_LIVEKIT_RTC_TCP_PORT:-7881}"
WEBHOOK_PORT="${CAAL_WEBHOOK_PORT:-8889}"
FRONTEND_PORT="${CAAL_FRONTEND_PORT:-3000}"
N8N_PORT="${CAAL_N8N_PORT:-5678}"

# Read-only mobile transcript access over Tailscale. The desktop remains the
# sole LiveKit/WebRTC client; only the Next.js frontend is published remotely.
TAILSCALE_BIN="${CAAL_TAILSCALE_BIN:-$(command -v tailscale || true)}"
REMOTE_ACCESS="${CAAL_REMOTE_ACCESS:-auto}"   # auto (on when Serve configured), true, false
PUBLIC_HTTPS_PORT="${CAAL_PUBLIC_HTTPS_PORT:-$FRONTEND_PORT}"
PUBLIC_HOST="${CAAL_PUBLIC_HOST:-}"           # auto-detected from Tailscale when empty

# auto: run n8n only once it has been installed via --install-n8n.
N8N_ENABLED="${CAAL_N8N_ENABLED:-auto}"

n8n_is_enabled() {
  case "$N8N_ENABLED" in
    false) return 1 ;;
    true) return 0 ;;
    *) [[ -f "$N8N_BIN" ]] ;;
  esac
}

# n8n starts before the agent so workflow tools exist at discovery time.
if n8n_is_enabled; then
  ALL_SERVICES=(model speech n8n livekit agent frontend)
  APP_SERVICES=(n8n livekit agent frontend)
else
  ALL_SERVICES=(model speech livekit agent frontend)
  APP_SERVICES=(livekit agent frontend)
fi
MODEL_SERVICES=(model speech)

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$PID_DIR" "$SUPERVISOR_DIR"

usage() {
  cat <<EOF
CAAL native Apple Silicon launcher

Commands:
  (no arguments)              Start and supervise every service
  --models                    Start and supervise the model, Whisper, and Kokoro
  --app                       Start LiveKit, agent, and frontend using healthy models
  --restart <service>         Restart model, speech, n8n, livekit, agent, or frontend
  --install-n8n               Install the bundled n8n runtime for workflow tools
  --setup-remote              Show how to publish CAAL on your tailnet
  --stop                      Stop every native service
  --status                    Show service PIDs and detect untracked CAAL processes
  --memory-report [hours]     Explain per-service RAM/Metal/swap growth (default: 24)
  --memory-sample             Record and print one deep attributed snapshot
  --attach [all|models|app]   Attach to a persistent supervisor (all by default)
  --logs [service]            Follow a service log; defaults to agent
  --help                      Show this help

Persistent runtime:
  Start commands run inside tmux so CAAL survives terminal and Codex sessions.
  CAAL_TMUX_SESSION           Base tmux session name ($TMUX_SESSION_BASE)

Port overrides:
  CAAL_MODEL_ID               Default HF repo id to serve ($MODEL_ID)
  CAAL_MODEL_PORT             OpenAI-compatible model API ($MODEL_PORT)
  CAAL_MODEL_WIRED_LIMIT_GB   Wired-memory cap ($MODEL_WIRED_LIMIT_GB)
  CAAL_MODEL_MEMORY_LIMIT_GB  Allocation cap ($MODEL_MEMORY_LIMIT_GB)
  CAAL_MODEL_PROMPT_CACHE_SIZE  Retained KV caches ($MODEL_PROMPT_CACHE_SIZE)
  CAAL_MODEL_PROMPT_CACHE_BYTES KV cache ceiling ($MODEL_PROMPT_CACHE_BYTES)
  CAAL_MODEL_PARALLEL         Prompt/decode concurrency ($MODEL_PARALLEL)
  CAAL_SPEECH_PORT            Whisper/Kokoro bridge ($SPEECH_PORT)
  CAAL_MLX_SPEECH_PYTHON      Existing Python environment with MLX speech packages
  CAAL_UV_PYTHON              Python version/interpreter used for native environments
  CAAL_LIVEKIT_PORT           LiveKit WebSocket/API ($LIVEKIT_PORT)
  CAAL_LIVEKIT_RTC_TCP_PORT   LiveKit RTC fallback TCP ($LIVEKIT_RTC_TCP_PORT)
  CAAL_WEBHOOK_PORT           Agent webhook API ($WEBHOOK_PORT)
  CAAL_FRONTEND_PORT          CAAL web interface ($FRONTEND_PORT)
  CAAL_N8N_PORT               n8n editor and webhooks ($N8N_PORT)
  CAAL_LOG_MAX_BYTES          Rotate a service log past this size (20 MiB)
  CAAL_MEMORY_SAMPLE_SECONDS  Lightweight system sample interval (60)
  CAAL_MEMORY_DEEP_SAMPLE_SECONDS  Per-process footprint interval (300)
  Memory guard:
  CAAL_MODEL_MEMORY_TRIP_GB        Ceiling when MLX is unreadable (8)
  CAAL_MODEL_ALLOCATION_TRIP_GB    MLX allocation ceiling (8)
  CAAL_MODEL_MEMORY_RECOVERY_GB    Footprint required after teardown (6)
  CAAL_MEMORY_GUARD                false to disable the guard entirely
  CAAL_MEMORY_CHECK_SECONDS        Seconds between guard samples (2)
  CAAL_MEMORY_TIGHT_READINGS       Consecutive trips before acting (2)
  CAAL_MEMORY_RECOVERY_TIMEOUT     Seconds to wait before reloading (20)

n8n workflow tools:
  CAAL_N8N_ENABLED            auto (run once installed), true, or false
  CAAL_N8N_VERSION            n8n release to install ($N8N_VERSION)
  Editor: http://127.0.0.1:$N8N_PORT — see docs/N8N-WORKFLOWS.md for setup

Remote access (Tailscale):
  CAAL_REMOTE_ACCESS          auto (on when Serve is configured), true, false
  CAAL_PUBLIC_HTTPS_PORT      HTTPS port for the mobile viewer ($PUBLIC_HTTPS_PORT)
  CAAL_PUBLIC_HOST            Override the auto-detected tailnet hostname
EOF
}

tailscale_available() {
  [[ -n "$TAILSCALE_BIN" ]] && "$TAILSCALE_BIN" status >/dev/null 2>&1
}

# MagicDNS name of this machine, e.g. macbook-pro.tail0cec88.ts.net
tailscale_dns_name() {
  [[ -n "$PUBLIC_HOST" ]] && { echo "$PUBLIC_HOST"; return 0; }
  tailscale_available || return 1
  "$TAILSCALE_BIN" status --json 2>/dev/null | /usr/bin/python3 -c '
import json, sys
try:
    name = json.load(sys.stdin).get("Self", {}).get("DNSName", "")
except Exception:
    sys.exit(1)
print(name.rstrip("."))
' 2>/dev/null
}

serve_is_configured() {
  tailscale_available || return 1
  "$TAILSCALE_BIN" serve status 2>/dev/null | grep -q ":$PUBLIC_HTTPS_PORT"
}

remote_access_enabled() {
  case "$REMOTE_ACCESS" in
    false) return 1 ;;
    true) tailscale_available ;;
    *) serve_is_configured ;;
  esac
}

status_remote_access() {
  local host
  if ! tailscale_available; then
    echo "remote access: tailscale unavailable"
    return
  fi
  host="$(tailscale_dns_name)"
  if remote_access_enabled && [[ -n "$host" ]]; then
    echo "mobile viewer: https://$host:$PUBLIC_HTTPS_PORT/mobile"
  else
    echo "remote access: off (run ./start-native.sh --setup-remote)"
  fi
}

# Prints the commands needed to publish CAAL on the tailnet. Serve edits
# require root, so this does not run them.
setup_remote_instructions() {
  local host
  tailscale_available || {
    echo "Tailscale is not running or not installed." >&2
    return 1
  }
  host="$(tailscale_dns_name)"
  [[ -n "$host" ]] || { echo "Could not determine the Tailscale DNS name." >&2; return 1; }

  cat <<EOF
Publish CAAL on your tailnet by running these once (they need root):

  sudo tailscale set --operator=\$USER
  tailscale serve --bg --https=$PUBLIC_HTTPS_PORT http://127.0.0.1:$FRONTEND_PORT

The first command lets your user manage Serve, so only it needs sudo.

On your phone (signed into the same tailnet), open:

  https://$host:$PUBLIC_HTTPS_PORT/mobile

To undo:

  tailscale serve --https=$PUBLIC_HTTPS_PORT off
EOF
}

require_tmux() {
  if [[ -z "$TMUX_BIN" ]] || ! "$TMUX_BIN" -V >/dev/null 2>&1; then
    echo "tmux is required for persistent native startup. Install it with: brew install tmux" >&2
    return 1
  fi
}

tmux_session_name() {
  case "$1" in
    all) echo "$TMUX_SESSION_BASE" ;;
    models) echo "$TMUX_SESSION_BASE-models" ;;
    app) echo "$TMUX_SESSION_BASE-app" ;;
  esac
}

tmux_session_is_running() {
  local session
  session="$(tmux_session_name "$1")"
  [[ -n "$TMUX_BIN" ]] && "$TMUX_BIN" has-session -t "=$session" 2>/dev/null
}

status_tmux_sessions() {
  local mode session found=false
  for mode in all models app; do
    session="$(tmux_session_name "$mode")"
    if tmux_session_is_running "$mode"; then
      printf 'tmux %-8s running (%s)\n' "$mode" "$session"
      found=true
    fi
  done
  [[ "$found" == true ]] || echo "tmux supervisors: stopped"
}

launchd_is_loaded() {
  launchctl print "system/$LAUNCHD_LABEL" >/dev/null 2>&1
}

launchd_is_running() {
  launchctl print "system/$LAUNCHD_LABEL" 2>/dev/null | grep -q 'state = running'
}

status_launchd() {
  if launchd_is_running; then
    echo "launchd supervisor: running ($LAUNCHD_LABEL)"
  elif launchd_is_loaded; then
    echo "launchd supervisor: loaded, not running ($LAUNCHD_LABEL)"
  else
    echo "launchd supervisor: not loaded"
  fi
}

stop_tmux_session() {
  local mode="$1" session
  session="$(tmux_session_name "$mode")"
  tmux_session_is_running "$mode" || return 0

  echo "Stopping tmux supervisor: $session"
  "$TMUX_BIN" send-keys -t "=$session" C-c 2>/dev/null || true
  for _ in $(seq 1 20); do
    tmux_session_is_running "$mode" || return 0
    sleep 0.5
  done

  echo "Supervisor did not exit cleanly; closing tmux session $session" >&2
  "$TMUX_BIN" kill-session -t "=$session" 2>/dev/null || true
}

stop_tmux_sessions() {
  local mode
  [[ -n "$TMUX_BIN" ]] || return 0
  for mode in "$@"; do stop_tmux_session "$mode"; done
}

is_service() {
  case "$1" in
    model|speech|n8n|livekit|agent|frontend) return 0 ;;
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
    model) echo "$MODEL_PORT" ;;
    speech) echo "$SPEECH_PORT" ;;
    n8n) echo "$N8N_PORT" ;;
    livekit) echo "$LIVEKIT_PORT" ;;
    agent) echo "$WEBHOOK_PORT" ;;
    frontend) echo "$FRONTEND_PORT" ;;
  esac
}

service_url() {
  case "$1" in
    model) echo "http://127.0.0.1:$MODEL_PORT/v1/models" ;;
    speech) echo "http://127.0.0.1:$SPEECH_PORT/health" ;;
    n8n) echo "http://127.0.0.1:$N8N_PORT/healthz" ;;
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
    model:*mlx_model_server.py*) return 0 ;;
    speech:*local_speech_server.py*) return 0 ;;
    n8n:*n8n/bin/n8n*) return 0 ;;
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
  stop_services frontend agent livekit n8n speech model
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
    model)
      ensure_mlx_model_environment
      executable="$MODEL_PYTHON"
      ;;
    speech)
      ensure_mlx_speech_environment
      executable="$SPEECH_PYTHON"
      ;;
    n8n)
      require_n8n_installed
      executable="$N8N_NODE_BIN"
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
  echo "Creating dedicated MLX speech environment..."
  "$UV_BIN" venv --python "$UV_PYTHON" "$MLX_SPEECH_VENV"
  "$UV_BIN" pip install --python "$SPEECH_PYTHON" \
    -r "$PROJECT_DIR/requirements-mlx-speech.txt"
}

ensure_mlx_model_environment() {
  if [[ -x "$MODEL_PYTHON" ]] && "$MODEL_PYTHON" -c \
    'import mlx_lm' >/dev/null 2>&1; then
    return 0
  fi
  [[ -n "$UV_BIN" && -x "$UV_BIN" ]] || {
    echo "Missing uv; install it or set CAAL_MLX_MODEL_PYTHON" >&2
    return 1
  }
  echo "Creating dedicated MLX model environment..."
  "$UV_BIN" venv --python "$UV_PYTHON" "$MLX_MODEL_VENV"
  "$UV_BIN" pip install --python "$MODEL_PYTHON" \
    -r "$PROJECT_DIR/requirements-mlx-model.txt"
}

require_n8n_installed() {
  [[ -f "$N8N_BIN" && -x "$N8N_NODE_BIN" ]] && return 0
  echo "n8n is not installed. Install it with: ./start-native.sh --install-n8n" >&2
  echo "Or skip it for this run with: CAAL_N8N_ENABLED=false ./start-native.sh" >&2
  return 1
}

install_n8n_node() {
  local url archive
  [[ -x "$N8N_NODE_BIN" ]] && return 0

  # n8n requires a newer Node than the frontend uses, so keep it self-contained.
  url="https://nodejs.org/dist/$N8N_NODE_VERSION/node-$N8N_NODE_VERSION-darwin-arm64.tar.gz"
  archive="$RUNTIME_DIR/node-$N8N_NODE_VERSION.tar.gz"
  echo "Downloading Node $N8N_NODE_VERSION for n8n..."
  curl -fsSL "$url" -o "$archive"
  mkdir -p "$N8N_NODE_DIR"
  tar -xzf "$archive" -C "$N8N_NODE_DIR" --strip-components=1
  rm -f "$archive"
}

install_n8n() {
  install_n8n_node
  echo "Installing n8n $N8N_VERSION (this downloads several hundred MB)..."
  mkdir -p "$N8N_PREFIX"
  # n8n pulls a very large dependency tree, so allow slow registry reads
  # rather than failing the whole install on one timeout.
  PATH="$N8N_NODE_DIR/bin:$PATH" "$N8N_NODE_DIR/bin/npm" install \
    --prefix "$N8N_PREFIX" "n8n@$N8N_VERSION" \
    --omit=dev --no-audit --no-fund \
    --fetch-retries=5 --fetch-retry-maxtimeout=120000 --fetch-timeout=600000
  [[ -f "$N8N_BIN" ]] || {
    echo "n8n install finished but $N8N_BIN is missing" >&2
    return 1
  }
  ensure_n8n_encryption_key
  echo "n8n installed. Start it with: ./start-native.sh"
}

ensure_n8n_encryption_key() {
  [[ -s "$N8N_KEY_FILE" ]] && return 0
  # Persisted so stored workflow credentials survive restarts.
  umask 077
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 >"$N8N_KEY_FILE"
  chmod 600 "$N8N_KEY_FILE"
}

prepare_frontend() {
  [[ -e "$NEXT_STANDALONE/public" ]] || ln -s ../../public "$NEXT_STANDALONE/public"
  mkdir -p "$NEXT_STANDALONE/.next"
  [[ -e "$NEXT_STANDALONE/.next/static" ]] || \
    ln -s ../../static "$NEXT_STANDALONE/.next/static"
}

rotate_log() {
  local path="$LOG_DIR/$1.log" size
  [[ -f "$path" ]] || return 0
  size="$(stat -f %z "$path" 2>/dev/null || echo 0)"
  if (( size >= LOG_MAX_BYTES )); then
    mv -f "$path" "$path.1"
  fi
}

start_service() {
  local name="$1"
  shift
  rotate_log "$name"
  if [[ "${CAAL_LAUNCHD:-false}" == "true" ]]; then
    "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  else
    nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  fi
  local pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  echo "  started $name (PID $pid)"
}

start_service_in() {
  local directory="$1" name="$2"
  shift 2
  rotate_log "$name"
  (
    cd "$directory"
    if [[ "${CAAL_LAUNCHD:-false}" == "true" ]]; then
      exec "$@"
    else
      exec nohup "$@"
    fi
  ) >>"$LOG_DIR/$name.log" 2>&1 &
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

wait_for_tmux_supervisor() {
  local mode="$1"
  for _ in $(seq 1 20); do
    supervisor_is_running "$mode" && return 0
    if ! tmux_session_is_running "$mode"; then
      echo "The tmux supervisor exited during startup. Check logs in $LOG_DIR." >&2
      return 1
    fi
    sleep 0.5
  done
  echo "The tmux supervisor did not register in time. Check logs in $LOG_DIR." >&2
  return 1
}

launch_tmux_supervisor() {
  local mode="$1" command session name
  require_tmux
  session="$(tmux_session_name "$mode")"

  if launchd_is_loaded; then
    echo "CAAL is managed by launchd service: $LAUNCHD_LABEL"
    echo "Status: ./launchd/manage.sh status"
    return 0
  fi

  if tmux_session_is_running "$mode"; then
    echo "CAAL $mode supervisor is already running in tmux session: $session"
    return 0
  fi

  printf -v command '%q --tmux-child' "$PROJECT_DIR/start-native.sh"
  case "$mode" in
    models) command+=" --models" ;;
    app) command+=" --app" ;;
  esac

  echo "Starting CAAL $mode supervisor in persistent tmux session: $session"
  "$TMUX_BIN" new-session -d -s "$session" -c "$PROJECT_DIR" "$command"
  wait_for_tmux_supervisor "$mode"

  case "$mode" in
    all)
      for name in "${ALL_SERVICES[@]}"; do wait_http "$name"; done
      echo "CAAL is ready: http://localhost:$FRONTEND_PORT"
      ;;
    models)
      for name in "${MODEL_SERVICES[@]}"; do wait_http "$name"; done
      echo "Model services are ready."
      ;;
    app)
      for name in "${APP_SERVICES[@]}"; do wait_http "$name"; done
      echo "CAAL is ready: http://localhost:$FRONTEND_PORT"
      ;;
  esac
  echo "Attach with: ./start-native.sh --attach $mode"
}

managed_tmux_mode_for_service() {
  local service="$1"
  if supervisor_is_running all && tmux_session_is_running all; then
    echo all
  elif [[ "$service" == "model" || "$service" == "speech" ]] && \
    supervisor_is_running models && tmux_session_is_running models; then
    echo models
  elif [[ "$service" == "livekit" || "$service" == "agent" || "$service" == "frontend" ]] && \
    supervisor_is_running app && tmux_session_is_running app; then
    echo app
  fi
}

restart_in_tmux() {
  local service="$1" mode session command old_pid="" new_pid=""
  mode="$(managed_tmux_mode_for_service "$service")"
  [[ -n "$mode" ]] || return 1
  session="$(tmux_session_name "$mode")"
  [[ -f "$PID_DIR/$service.pid" ]] && old_pid="$(<"$PID_DIR/$service.pid")"

  printf -v command '%q --tmux-child --restart %q' "$PROJECT_DIR/start-native.sh" "$service"
  "$TMUX_BIN" new-window -d -t "=$session" -n "restart-$service" "$command"

  for _ in $(seq 1 120); do
    if [[ -f "$PID_DIR/$service.pid" ]]; then
      new_pid="$(<"$PID_DIR/$service.pid")"
      if [[ "$new_pid" != "$old_pid" ]] && pid_matches_service "$service" "$new_pid" && \
        curl -fsS "$(service_url "$service")" >/dev/null 2>&1; then
        echo "$service restarted successfully"
        return 0
      fi
    fi
    sleep 1
  done
  echo "$service did not restart successfully; see $LOG_DIR/$service.log" >&2
  return 1
}

start_named() {
  local name="$1"
  validate_service_dependency "$name"
  case "$name" in
    model)
      echo "Starting MLX model server: $MODEL_ID → http://127.0.0.1:$MODEL_PORT/v1"
      start_service model env \
        CAAL_MODEL_WIRED_LIMIT_GB="$MODEL_WIRED_LIMIT_GB" \
        CAAL_MODEL_MEMORY_LIMIT_GB="$MODEL_MEMORY_LIMIT_GB" \
        "$MODEL_PYTHON" "$PROJECT_DIR/scripts/mlx_model_server.py" \
        --model "$MODEL_ID" \
        --host 127.0.0.1 --port "$MODEL_PORT" \
        --prompt-concurrency "$MODEL_PARALLEL" \
        --decode-concurrency "$MODEL_PARALLEL" \
        --prompt-cache-size "$MODEL_PROMPT_CACHE_SIZE" \
        --prompt-cache-bytes "$MODEL_PROMPT_CACHE_BYTES"
      ;;
    speech)
      echo "Starting MLX Whisper + MLX Kokoro → http://127.0.0.1:$SPEECH_PORT"
      start_service speech env \
        SPEECH_HOST="127.0.0.1" SPEECH_PORT="$SPEECH_PORT" \
        "$SPEECH_PYTHON" "$PROJECT_DIR/local_speech_server.py"
      ;;
    n8n)
      ensure_n8n_encryption_key
      mkdir -p "$N8N_DATA_DIR"
      echo "Starting n8n → http://127.0.0.1:$N8N_PORT"
      start_service n8n env \
        N8N_USER_FOLDER="$N8N_DATA_DIR" \
        N8N_PORT="$N8N_PORT" \
        N8N_LISTEN_ADDRESS="127.0.0.1" \
        N8N_HOST="127.0.0.1" \
        N8N_PROTOCOL="http" \
        N8N_EDITOR_BASE_URL="http://127.0.0.1:$N8N_PORT/" \
        WEBHOOK_URL="http://127.0.0.1:$N8N_PORT/" \
        N8N_ENCRYPTION_KEY="$(<"$N8N_KEY_FILE")" \
        N8N_DIAGNOSTICS_ENABLED="false" \
        N8N_VERSION_NOTIFICATIONS_ENABLED="false" \
        N8N_SECURE_COOKIE="false" \
        N8N_RUNNERS_ENABLED="true" \
        GENERIC_TIMEZONE="${CAAL_TIMEZONE:-Asia/Taipei}" \
        PATH="$N8N_NODE_DIR/bin:$PATH" \
        "$N8N_NODE_BIN" "$N8N_BIN" start
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
        CAAL_PROJECT_DIR="$PROJECT_DIR" \
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
      "http://127.0.0.1:$SPEECH_PORT/v1/models?model_name=${CAAL_WHISPER_MODEL:-mlx-community/distil-whisper-large-v3}" \
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
  stop_services speech model >/dev/null 2>&1 || true
  unregister_supervisor models
}

cleanup_app() {
  trap - EXIT
  stop_services frontend agent livekit n8n >/dev/null 2>&1 || true
  unregister_supervisor app
}

validate_port CAAL_MODEL_PORT "$MODEL_PORT"
if ! [[ "$MODEL_WIRED_LIMIT_GB" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Invalid CAAL_MODEL_WIRED_LIMIT_GB: $MODEL_WIRED_LIMIT_GB" >&2
  exit 1
fi
if ! [[ "$MODEL_PARALLEL" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid CAAL_MODEL_PARALLEL: $MODEL_PARALLEL" >&2
  exit 1
fi
validate_port CAAL_SPEECH_PORT "$SPEECH_PORT"
validate_port CAAL_LIVEKIT_PORT "$LIVEKIT_PORT"
validate_port CAAL_LIVEKIT_RTC_TCP_PORT "$LIVEKIT_RTC_TCP_PORT"
validate_port CAAL_WEBHOOK_PORT "$WEBHOOK_PORT"
validate_port CAAL_FRONTEND_PORT "$FRONTEND_PORT"
validate_port CAAL_N8N_PORT "$N8N_PORT"

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
export OPENAI_BASE_URL="http://127.0.0.1:$MODEL_PORT/v1"
# Only the default for a fresh install. A model saved in settings wins, which is
# what makes switching from the web UI take effect on the next call.
export OPENAI_MODEL="$MODEL_ID"
export STT_PROVIDER="speaches"
export SPEACHES_URL="http://127.0.0.1:$SPEECH_PORT"
export WHISPER_MODEL="${CAAL_WHISPER_MODEL:-mlx-community/distil-whisper-large-v3}"
export TTS_PROVIDER="kokoro"
export KOKORO_URL="http://127.0.0.1:$SPEECH_PORT"
export TTS_MODEL="${CAAL_KOKORO_MODEL:-mlx-community/Kokoro-82M-bf16}"
export TTS_VOICE="${CAAL_KOKORO_VOICE:-af_heart}"
export TIMEZONE="${CAAL_TIMEZONE:-Asia/Taipei}"
export TIMEZONE_DISPLAY="${CAAL_TIMEZONE_DISPLAY:-Taipei Time}"

# Point the agent at the local n8n MCP endpoint. N8N_MCP_TOKEN stays in .env,
# which load_dotenv reads without overriding what is exported here.
if n8n_is_enabled; then
  export N8N_MCP_URL="${N8N_MCP_URL:-http://127.0.0.1:$N8N_PORT/mcp-server/http}"
fi

if [[ "$TMUX_CHILD" != true ]]; then
  case "${1:-}" in
    "")
      launch_tmux_supervisor all
      exit 0
      ;;
    --models)
      launch_tmux_supervisor models
      exit 0
      ;;
    --app)
      launch_tmux_supervisor app
      exit 0
      ;;
    --restart)
      restart_service="${2:-}"
      is_service "$restart_service" || {
        echo "Usage: ./start-native.sh --restart <model|speech|n8n|livekit|agent|frontend>" >&2
        exit 2
      }
      if restart_in_tmux "$restart_service"; then
        exit 0
      fi
      ;;
  esac
fi

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --install-n8n)
    install_n8n
    exit 0
    ;;
  --setup-remote)
    setup_remote_instructions
    exit 0
    ;;
  --stop)
    if [[ "$TMUX_CHILD" != true ]] && launchd_is_loaded; then
      exec "$PROJECT_DIR/launchd/manage.sh" stop
    fi
    stop_tmux_sessions all app models
    stop_supervisors all app models
    stop_all
    exit 0
    ;;
  --status)
    for name in "${ALL_SERVICES[@]}"; do status_one "$name"; done
    status_supervisors
    status_tmux_sessions
    status_launchd
    status_remote_access
    exit 0
    ;;
  --memory-report)
    report_hours="${2:-24}"
    exec "$AGENT_PYTHON" "$PROJECT_DIR/scripts/native_memory_monitor.py" \
      --project "$PROJECT_DIR" report --hours "$report_hours"
    ;;
  --memory-sample)
    exec "$AGENT_PYTHON" "$PROJECT_DIR/scripts/native_memory_monitor.py" \
      --project "$PROJECT_DIR" sample
    ;;
  --attach)
    attach_mode="${2:-all}"
    case "$attach_mode" in
      all|models|app) ;;
      *)
        echo "Usage: ./start-native.sh --attach [all|models|app]" >&2
        exit 2
        ;;
    esac
    require_tmux
    attach_session="$(tmux_session_name "$attach_mode")"
    if ! tmux_session_is_running "$attach_mode"; then
      echo "tmux session is not running: $attach_session" >&2
      exit 1
    fi
    exec "$TMUX_BIN" attach-session -t "=$attach_session"
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
      echo "Usage: ./start-native.sh --restart <model|speech|n8n|livekit|agent|frontend>" >&2
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
    stop_services speech model
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
echo "Model: $MODEL_ID (wired <= ${MODEL_WIRED_LIMIT_GB} GiB)"
echo "Whisper: $WHISPER_MODEL"
echo "Kokoro: $TTS_MODEL ($TTS_VOICE)"
echo "Press Ctrl-C to stop all native services."

monitor_services "${ALL_SERVICES[@]}"
