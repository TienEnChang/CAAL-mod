# CAAL

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![LiveKit](https://img.shields.io/badge/LiveKit-Agents-purple.svg)](https://docs.livekit.io/agents/)

> **Self-hosted voice assistant you actually own.  Secure by design - the LLM never sees your API keys.**

CAAL is an open-source voice assistant built on [LiveKit Agents](https://docs.livekit.io/agents/) that runs entirely on your hardware. Your voice, your data, your credentials — all on your network.

![CAAL Voice Assistant](frontend/.github/assets/readme-hero.webp)

---

### Why CAAL?

**Secure by architecture.** The model never sees your API keys. Ever. Credentials live in [n8n's](https://n8n.io/) encrypted credential store. The LLM sends parameters to a webhook, the workflow handles auth. Even if a prompt injection succeeds, the model can only call pre-built workflows — no shell access, no curl, no ability to transmit data. It's an air gap for the LLM. Every tool in the [registry](https://github.com/CoreWorxLab/caal-tools) goes through automated security review and human approval before it's available to install.

**Purpose-built model.** CAAL ships with [`caal-ministral`](https://ollama.com/coreworxlab/caal-ministral) — a fine-tuned 8B model trained specifically for voice tool calling. It knows how to chain tools together and respond naturally. The LLM is one piece of the architecture, not the architecture. It handles decisions that code can't — which tool to call, what parameters to use, when to chain steps. Everything else is code. That's why an 8B model works.
```bash
ollama pull coreworxlab/caal-qwen3.5-9b
```

**Infinitely extensible.** Any [n8n](https://n8n.io/) workflow becomes a voice-activated tool. Query APIs, automate your life, then share your tools with the community via the [CAAL Tool Registry](https://github.com/CoreWorxLab/caal-tools). Tools follow a suite convention — fewer tools, better accuracy, more reliable routing.

**Local by default.** Runs fully on your network with [Ollama](https://ollama.ai/). No accounts, no telemetry, no cloud dependency. Want to use [Groq](https://groq.com/), [OpenRouter](https://openrouter.ai/), or any OpenAI-compatible API? Your choice. Your credentials and tool executions never leave your network regardless.

---

## Features

- **Tool Registry** — Browse and install community tools with one click. Every submission goes through automated security review and human approval
- **Tool Chaining** — Sequential multi-tool calls in one prompt. The model uses real data from each step to inform the next
- **n8n Workflows** — Any workflow becomes a tool. Visual, inspectable, shareable, auditable through n8n's execution history
- **Flexible Providers** — Ollama, Groq, OpenRouter, or any OpenAI-compatible API. Speaches or Groq for STT. Kokoro or Piper for TTS
- **Short-Term Memory** — Store and recall information across sessions
- **Internationalization** — English, French, Italian, with more coming
- **Wake Word** — "Hey Cal" via OpenWakeWord
- **Web Search** — DuckDuckGo integration for real-time information
- **Mobile App** — Android client available from [Releases](https://github.com/CoreWorxLab/caal/releases)
- **Webhook API** — REST API for announcements, settings, and external triggers

---

## Quick Start

```bash
git clone https://github.com/CoreWorxLab/caal.git
cd caal
cp .env.example .env
nano .env  # Set CAAL_HOST_IP to your server's LAN IP

# If using Ollama (recommended)
ollama pull coreworxlab/caal-ministral:latest

docker compose up -d
```

Open `https://YOUR_SERVER_IP:3443` and complete the setup wizard.

> Requires Docker with [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) and 8GB+ VRAM. For CPU-only or Apple Silicon setups, see [Deployment Options](#deployment-options).

## Deployment Options

| Mode | Hardware | Command | Guide |
|------|----------|---------|-------|
| **GPU** | Linux + NVIDIA GPU | `docker compose up -d` | Quick Start above |
| **CPU-only** | Any Docker host | `docker compose -f docker-compose.cpu.yaml up -d` | [Wiki: CPU Mode](https://github.com/CoreWorxLab/CAAL/wiki) |
| **Apple Silicon** | M1/M2/M3/M4 Mac | `./start-apple.sh` | [Apple Silicon Guide](docs/APPLE-SILICON.md) |
| **Distributed** | GPU server + frontend | See guide | [Distributed Guide](docs/DISTRIBUTED-DEPLOYMENT.md) |

### Fully native Apple Silicon (no Docker)

After the native dependencies and frontend build are installed, run the complete
stack with:

```bash
./start-native.sh
```

For faster development, keep the Metal-backed models loaded in one terminal and
restart the app services independently in another:

```bash
# Terminal 1: Qwen, Whisper, and Kokoro
./start-native.sh --models

# Terminal 2: LiveKit, agent, and frontend
./start-native.sh --app

# Restart one changed service without reloading the models
./start-native.sh --restart frontend
```

Use `./start-native.sh --help` for status, logs, stop commands, service names,
and configurable port environment variables.

Native launchd installs continuously record lightweight system memory samples
every minute and attributed macOS `footprint` samples every five minutes. The
diagnostics separate Qwen, Whisper/Kokoro, agent workers, n8n, LiveKit, and the
frontend; they also track Metal/IOAccelerator allocations, compressed or
swapped pages, system swap, and Qwen prompt-cache growth. Inspect the last day:

```bash
./start-native.sh --memory-report 24
```

Use `./start-native.sh --memory-sample` for an immediate deep snapshot. Samples
are kept in the ignored, rotating `.native/logs/memory.jsonl` file (20 MB plus
one rotated generation), so no personal transcript or prompt content is logged.

During an active call the native agent watches two trip signals:

- Qwen's own allocation reaching 6 GiB
- macOS entering its integrated critical memory-pressure state

MLX allocation is the early, Qwen-specific boundary. The macOS signal comes
from `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE`, which integrates the VM system's
reclaimable memory, compression and swapping decisions instead of making CAAL
derive a second pressure policy from free RAM and swap.

Prevention rests on the cap rather than the guard: Qwen is launched with its
wired memory clamped to 6 GiB (`CAAL_QWEN_WIRED_LIMIT_GB`), well below the
11.84 GiB mlx-lm would otherwise pin. Wired pages cannot be paged out, so an
unclamped Qwen makes room by evicting every other process to swap. Inside the
cap it reclaims its own cache instead.

At the MLX boundary the session is marked terminated, further turns are
refused, and normal teardown clears the retained cache. If allocation does not
fall below 4.5 GiB within 20 seconds, Qwen is restarted. At critical macOS
pressure Qwen is restarted first so the room teardown and reconnect have enough
memory to complete. In either case the desktop reconnects a fresh agent job to
the same conversation and rehydrates its history.

When a call ends, the agent asks the local Qwen server to drop the prompt cache
that call built up. The system prompt embeds the current wall-clock minute, so a
finished call's KV cache can never be reused by a later one; releasing it at
teardown returns the memory immediately instead of holding it until the next
call happens to evict it. Reuse *within* a call is unaffected. This only applies
to a Qwen server on this machine - a remote OpenAI-compatible endpoint is left
alone.

The same teardown asks the local MLX speech bridge to clear reusable allocator
buffers after STT and TTS finish. Whisper and Kokoro remain loaded, so the next
call keeps the warm model baseline without retaining transient inference memory.

To run n8n workflow tools natively, install the bundled n8n runtime once:

```bash
./start-native.sh --install-n8n
```

It then starts and stops with the rest of the stack, with the editor at
`http://127.0.0.1:5678`. See [n8n Workflows](docs/N8N-WORKFLOWS.md) for
enabling MCP access and connecting CAAL.

#### Native backup and restore

Git contains the application and rebuildable environment. Keep one small,
encrypted state backup for conversations, memory, runtime settings and
credentials, n8n workflows, custom prompts, and greetings:

```bash
./backup-native.sh /path/to/caal-native.caalbak
```

SQLite databases are snapshotted consistently while CAAL is running. Models,
virtual environments, binaries, caches, logs, and process files are excluded.
The archive uses authenticated AES-256-GCM encryption. On first use, the script
creates a strong random `CAAL Native Backup Key` in the macOS login Keychain;
later backups and restores retrieve it automatically. A normal macOS account
password change keeps the Keychain item—and existing archives—working.

On a fresh machine, clone the repository and install its dependencies first.
Stop CAAL before restoring state:

```bash
./start-native.sh --stop
./restore-native.sh /path/to/caal-native.caalbak
./start-native.sh
```

Restore refuses to replace existing state unless `--force` is supplied. The
n8n MCP token and API key return through `settings.json`; the separate n8n
encryption key is restored alongside its database so stored workflow
credentials remain decryptable. Backup files contain sensitive credentials and
personal data even though they are encrypted. For a fresh-Mac disaster restore,
restore the login Keychain from Time Machine before running this script. Use
`--prompt` or `--password-file FILE` only when a portable key is intentionally
managed outside macOS Keychain.

### HTTPS & Network

HTTPS is enabled by default with auto-generated self-signed certificates. This is required because browsers block microphone access on non-`localhost` HTTP.

To avoid the browser certificate warning, trust the auto-generated cert:
```bash
./trust-cert.sh
```
This works on macOS and Linux (Debian/Ubuntu, RHEL/Fedora, Arch, Chrome, Firefox). Pass `--yes` to skip the confirmation prompt. On Apple Silicon, `start-apple.sh` runs it automatically.

Alternatively, for browser-trusted certs use [mkcert](https://github.com/FiloSottile/mkcert):
```bash
mkcert -install && mkcert 192.168.1.100
mkdir -p certs && mv 192.168.1.100.pem certs/server.crt && mv 192.168.1.100-key.pem certs/server.key
```

For remote access via [Tailscale](https://tailscale.com/), set `HTTPS_DOMAIN` in `.env` to your Tailscale domain. See the [wiki](https://github.com/CoreWorxLab/CAAL/wiki) for details.

> The mobile app connects via LiveKit directly and doesn't require HTTPS.

---

## Documentation

| Resource | Description |
|----------|-------------|
| **[Wiki](https://github.com/CoreWorxLab/CAAL/wiki)** | Full documentation — architecture, configuration, deployment |
| [n8n Workflows](docs/N8N-WORKFLOWS.md) | Creating and connecting workflow tools |
| [Apple Silicon](docs/APPLE-SILICON.md) | Running on M1/M2/M3/M4 Macs |
| [Distributed Deployment](docs/DISTRIBUTED-DEPLOYMENT.md) | Split GPU backend and frontend |
| [Internationalization](docs/I18N.md) | Adding language support |
| [Tool Registry](https://github.com/CoreWorxLab/caal-tools) | Browse and submit community tools |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and contribution guidelines |

---

## Architecture

```
                          https://<IP>:3443    https://<IP>:7443
                                │                     │
                          ┌─────┴─────────────────────┴─────┐
                          │           nginx (TLS)           │
                          │         :3443  :7443            │
                          └─────┬─────────────────┬─────────┘
                                │                 │
┌───────────────────────────────┼─────────────────┼───────────────────┐
│  Docker Compose Stack         │                 │                   │
│                               │                 │                   │
│  ┌────────────┐         ┌─────┴──────┐  ┌───────┴────┐  ┌────────┐  │
│  │  Speaches  │         │  Frontend  │  │  LiveKit   │  │ Kokoro │  │
│  │(STT, GPU)  │         │  (Next.js) │  │   Server   │  │ (TTS)  │  │
│  │   :8000    │         │   :3000    │  │   :7880    │  │ :8880  │  │
│  └─────┬──────┘         └─────┬──────┘  └─────┬──────┘  └───┬────┘  │
│        │                      │               │             │       │
│        └──────────────────────┼───────────────┼─────────────┘       │
│                               │               │                     │
│                         ┌─────┴───────────────┴─────┐               │
│                         │         Agent             │               │
│                         │    (Voice Pipeline)       │               │
│                         │    :8889 (webhooks)       │               │
│                         └─────────┬─────────────────┘               │
│                                   │                                 │
└───────────────────────────────────┼─────────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
     ┌─────┴─────┐           ┌──────┴──────┐          ┌──────┴──────┐
     │   Ollama  │           │     n8n     │          │    Home     │
     │Groq / OR  │           │  Workflows  │          │  Assistant  │
     └───────────┘           └─────────────┘          └─────────────┘
                      External Services (via MCP)
```

---

## Community

CAAL is built in the open. If you build a tool, we see the PR. If you find a bug, we see the issue.

- **[Tool Registry](https://github.com/CoreWorxLab/caal-tools)** — Browse, install, and share tools
- **[Discussions](https://github.com/CoreWorxLab/CAAL/discussions)** — Feature requests, questions, feedback
- **[Fine-tuned Model](https://ollama.com/coreworxlab/caal-ministral)** — `ollama pull coreworxlab/caal-ministral`

---

## Related Projects

- [LiveKit Agents](https://github.com/livekit/agents) - Voice agent framework
- [Speaches](https://github.com/speaches-ai/speaches) - Faster-Whisper STT + Piper TTS
- [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) - Kokoro TTS server
- [Ollama](https://ollama.ai/) - Local LLM server
- [Groq](https://groq.com/) - Fast cloud LLM inference
- [OpenRouter](https://openrouter.ai/) - Unified API for 200+ models
- [n8n](https://n8n.io/) - Workflow automation

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License — see [LICENSE](LICENSE) for details.
