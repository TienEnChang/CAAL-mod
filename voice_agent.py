#!/usr/bin/env python3
"""
CAAL Voice Framework - Voice Agent
==================================

A voice assistant with MCP integrations for n8n workflows.

Usage:
    python voice_agent.py dev

Configuration:
    - .env: Environment variables (MCP URL, model settings)
    - prompt/default.md: Agent system prompt

Environment Variables:
    SPEACHES_URL        - Speaches STT service URL (default: "http://speaches:8000")
    KOKORO_URL          - Kokoro TTS service URL (default: "http://kokoro:8880")
    WHISPER_MODEL       - Whisper model for STT (default: "Systran/faster-whisper-small")
    TTS_VOICE           - Kokoro voice name (default: "af_heart")
    OLLAMA_MODEL        - Ollama model name (default: "ministral-3:8b")
    OLLAMA_THINK        - Enable thinking mode (default: "false")
    TIMEZONE            - Timezone for date/time (default: "Pacific Time")
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
import uuid

import requests

# Add src directory to path for local development
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv

# Load environment variables from .env
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, ".env"))

from livekit import agents, rtc  # noqa: E402
from livekit.agents import Agent, AgentSession, llm, mcp  # noqa: E402
from livekit.plugins import groq as groq_plugin  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

from caal import CAALLLM  # noqa: E402
from caal.context import ToolContext  # noqa: E402
from caal.conversation_summary import (  # noqa: E402
    checkpoint_conversation_summary,
    recall_block,
)
from caal.conversations import ConversationStore  # noqa: E402
from caal.integrations import (  # noqa: E402
    MEMORY_SHORT_TOOL_DEF,
    WEB_SEARCH_TOOL_DEF,
    MemoryTools,
    WebSearchTools,
    create_hass_tools,
    detect_hass_tool_prefix,
    discover_n8n_workflows,
    initialize_mcp_servers,
    load_mcp_config,
    mcp_url_to_base_url,
)
from caal.llm import ToolDataCache, discover_tools, llm_node  # noqa: E402
from caal.lmstudio_model import reload_local_lmstudio_model  # noqa: E402
from caal.memory import ShortTermMemory  # noqa: E402
from caal.memory_guard import (  # noqa: E402
    MemoryGuardConfig,
    MemoryTrip,
    guard_loop,
    wait_for_recovery,
)
from caal.prompt_lifecycle import (  # noqa: E402
    build_session_context,
    warm_prompt_prefix,
)
from caal.speech_cache import clear_local_speech_cache  # noqa: E402
from caal.stt import PreviewStreamAdapter, WakeWordGatedSTT  # noqa: E402
from caal.tts.sync_openai_tts import SyncOpenAITTS  # noqa: E402

# Configure logging - LiveKit adds LogQueueHandler to root in worker processes,
# so we use non-propagating loggers with our own handler to avoid duplicates
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter("%(message)s"))

# voice-agent logger (this file)
logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(_log_handler)

# caal package logger (src/caal/*)
_caal_logger = logging.getLogger("caal")
_caal_logger.setLevel(logging.INFO)
_caal_logger.propagate = False
_caal_logger.addHandler(_log_handler)

# Suppress verbose logs from dependencies
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai._base_client").setLevel(logging.WARNING)
logging.getLogger("groq._base_client").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("livekit").setLevel(logging.WARNING)
logging.getLogger("livekit_api").setLevel(logging.WARNING)
logging.getLogger("livekit.agents.tts").setLevel(logging.ERROR)  # Suppress "no request_id" warnings
logging.getLogger("livekit.agents.voice").setLevel(logging.WARNING)
logging.getLogger("livekit.plugins.openai.tts").setLevel(logging.WARNING)

# =============================================================================
# Configuration
# =============================================================================

# Infrastructure config (from .env only - URLs, tokens, etc.)
SPEACHES_URL = os.getenv("SPEACHES_URL", "http://speaches:8000")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "Systran/faster-whisper-small")
KOKORO_URL = os.getenv("KOKORO_URL", "http://kokoro:8880")
PIPER_URL = os.getenv("PIPER_URL", SPEACHES_URL)  # Separate URL for Piper TTS
TTS_MODEL = os.getenv("TTS_MODEL", "kokoro")
logger.info(f"[TTS Config] KOKORO_URL={KOKORO_URL}, PIPER_URL={PIPER_URL}, TTS_MODEL={TTS_MODEL}")
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").lower() == "true"
TIMEZONE_ID = os.getenv("TIMEZONE", "America/Los_Angeles")
TIMEZONE_DISPLAY = os.getenv("TIMEZONE_DISPLAY", "Pacific Time")

# Import settings module for runtime-configurable values
from caal import settings as settings_module  # noqa: E402


def get_wake_greetings(language: str) -> list[str]:
    """Get wake greetings from file for the given language."""
    return settings_module.load_greetings(language)


def get_runtime_settings() -> dict:
    """Get runtime-configurable settings.

    These can be changed via the settings UI without rebuilding.
    Falls back to .env values for backwards compatibility.

    Priority: settings.json (explicit) > .env > DEFAULT_SETTINGS
    """
    settings = settings_module.load_settings()
    user_settings = settings_module.load_user_settings()  # Only explicitly set values

    return {
        # Language
        "language": settings.get("language", "en"),
        # TTS settings
        "tts_provider": user_settings.get("tts_provider") or os.getenv("TTS_PROVIDER", "kokoro"),
        "tts_voice_kokoro": settings.get("tts_voice_kokoro") or os.getenv("TTS_VOICE", "am_puck"),
        "tts_voice_piper": settings.get("tts_voice_piper") or "speaches-ai/piper-en_US-ryan-high",
        # STT Provider settings
        "stt_provider": user_settings.get("stt_provider") or os.getenv("STT_PROVIDER", "speaches"),
        # LLM Provider settings - .env overrides default, user setting overrides .env
        "llm_provider": user_settings.get("llm_provider") or os.getenv("LLM_PROVIDER", "ollama"),
        "temperature": settings.get("temperature", float(os.getenv("OLLAMA_TEMPERATURE", "0.15"))),
        # Ollama settings
        "ollama_host": (
            user_settings.get("ollama_host")
            or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        ),
        "ollama_model": (
            user_settings.get("ollama_model")
            or os.getenv("OLLAMA_MODEL", "ministral-3:8b")
        ),
        "num_ctx": settings.get("num_ctx", int(os.getenv("OLLAMA_NUM_CTX", "8192"))),
        "think": OLLAMA_THINK,  # Only applies to Ollama
        # Groq settings
        "groq_api_key": settings.get("groq_api_key") or os.getenv("GROQ_API_KEY", ""),
        "groq_model": (
            user_settings.get("groq_model")
            or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        ),
        # OpenAI-compatible settings
        "openai_base_url": (
            user_settings.get("openai_base_url")
            or os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
        ),
        "openai_api_key": (
            settings.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")
        ),
        "openai_model": (
            os.getenv("CAAL_LMSTUDIO_INSTANCE_ID")
            or user_settings.get("openai_model")
            or os.getenv("OPENAI_MODEL", "")
        ),
        # OpenRouter settings
        "openrouter_api_key": (
            settings.get("openrouter_api_key")
            or os.getenv("OPENROUTER_API_KEY", "")
        ),
        "openrouter_model": (
            user_settings.get("openrouter_model")
            or os.getenv("OPENROUTER_MODEL", "openai/gpt-4")
        ),
        # Shared settings
        "max_turns": settings.get("max_turns", int(os.getenv("OLLAMA_MAX_TURNS", "20"))),
        "tool_cache_size": settings.get("tool_cache_size", int(os.getenv("TOOL_CACHE_SIZE", "3"))),
        # Turn detection settings
        "allow_interruptions": settings.get("allow_interruptions", True),
        "min_endpointing_delay": settings.get("min_endpointing_delay", 0.5),
    }


def load_prompt(language: str = "en") -> str:
    """Load only the call-invariant system instructions."""
    return settings_module.load_stable_prompt(
        timezone_display=TIMEZONE_DISPLAY,
        language=language,
    )


# =============================================================================
# Agent Definition
# =============================================================================

# Type alias for tool status callback
ToolStatusCallback = callable  # async (bool, list[str], list[dict]) -> None


class VoiceAssistant(MemoryTools, WebSearchTools, Agent):
    """Voice assistant with MCP tools, web search, and short-term memory."""

    def __init__(
        self,
        caal_llm: CAALLLM,
        language: str = "en",
        mcp_servers: dict[str, mcp.MCPServerHTTP] | None = None,
        n8n_workflow_tools: list[dict] | None = None,
        n8n_workflow_name_map: dict[str, str] | None = None,
        n8n_base_url: str | None = None,
        on_tool_status: ToolStatusCallback | None = None,
        tool_cache_size: int = 3,
        max_turns: int = 20,
        hass_tool_definitions: list[dict] | None = None,
        hass_tool_callables: dict | None = None,
        short_term_memory: ShortTermMemory | None = None,
        chat_ctx: llm.ChatContext | None = None,
        session_context: str | None = None,
        inference_lock: asyncio.Lock | None = None,
    ) -> None:
        stable_prompt = load_prompt(language=language)
        super().__init__(
            instructions=stable_prompt,
            llm=caal_llm,  # Satisfies LLM interface requirement
            chat_ctx=chat_ctx,
        )

        # Store provider for llm_node access
        self._provider = caal_llm.provider_instance
        self._stable_prompt = stable_prompt
        self._session_context = session_context
        self._inference_lock = inference_lock or asyncio.Lock()

        # All MCP servers (for multi-MCP support)
        # Named _caal_mcp_servers to avoid conflict with LiveKit's internal _mcp_servers handling
        self._caal_mcp_servers = mcp_servers or {}

        # n8n-specific for workflow execution (n8n uses webhook-based execution)
        self._n8n_workflow_tools = n8n_workflow_tools or []
        self._n8n_workflow_name_map = n8n_workflow_name_map or {}
        self._n8n_base_url = n8n_base_url

        # Home Assistant tools (only if HASS is connected)
        self._hass_tool_definitions = hass_tool_definitions or []
        self._hass_tool_callables = hass_tool_callables or {}
        # Use the same canonical schemas as non-LiveKit startup discovery. The
        # discovery layer de-duplicates these over LiveKit's reflected schemas.
        self._agent_tool_definitions = [
            MEMORY_SHORT_TOOL_DEF,
            WEB_SEARCH_TOOL_DEF,
        ]

        # Callback for publishing tool status to frontend
        self._on_tool_status = on_tool_status

        # Context management: tool data cache and sliding window
        self._tool_data_cache = ToolDataCache(max_entries=tool_cache_size)
        self._max_turns = max_turns

        # Short-term memory for persistent context (MemoryTools mixin requirement)
        self._short_term_memory = short_term_memory

        # Compatibility hook for callers that have not yet adopted the frozen
        # session-context block. Voice calls include recall in that block.
        self._conversation_recall = ""
        self._session_prefix_warm_task: asyncio.Task | None = None

        # Set to the trip reason when the memory guard ends this session. The
        # session is finished at that point, so no further turn may reach the
        # model - another generation is exactly what the machine cannot afford.
        self._memory_terminated: str | None = None

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Custom LLM node using provider-agnostic interface."""
        if self._memory_terminated:
            logger.warning(
                f"Refusing turn, session ended for memory: {self._memory_terminated}"
            )
            yield (
                "I ran out of memory and had to end that session. "
                "Start a new call and I'll pick up where we left off."
            )
            return

        # A barge-in may finish STT before greeting-time prefill completes.
        # Reuse that request instead of starting a competing prediction; this
        # holds active Qwen concurrency at one and preserves the prepared cache.
        if (
            self._session_prefix_warm_task
            and not self._session_prefix_warm_task.done()
        ):
            logger.info("First user turn waiting for session-prefix warm-up")
            await self._session_prefix_warm_task

        async with self._inference_lock:
            async for chunk in llm_node(
                self,
                chat_ctx,
                provider=self._provider,
                tool_data_cache=self._tool_data_cache,
                short_term_memory=self._short_term_memory,
                max_turns=self._max_turns,
            ):
                yield chunk


# =============================================================================
# Agent Entrypoint
# =============================================================================

async def entrypoint(ctx: agents.JobContext) -> None:
    """Main entrypoint for the voice agent."""
    # Note: Webhook server is started in background thread at agent startup (main block)
    # This ensures /setup/status is available before users connect

    # Debug: log TTS config in subprocess
    logger.info(f"[JOB] TTS Config: KOKORO_URL={KOKORO_URL}, TTS_MODEL={TTS_MODEL}")

    logger.debug(f"Joining room: {ctx.room.name}")
    await ctx.connect()

    # Load MCP servers from config
    mcp_servers = {}
    mcp_errors = []
    try:
        mcp_configs = load_mcp_config()
        mcp_servers, mcp_errors = await initialize_mcp_servers(mcp_configs)
    except Exception as e:
        logger.error(f"Failed to load MCP config: {e}")
        mcp_configs = []  # Ensure mcp_configs is defined for later use

    # Send MCP connection errors to frontend
    if mcp_errors:
        error_messages = []
        for err in mcp_errors:
            # Friendly names for known servers
            if err.name == "n8n":
                error_messages.append(
                    "n8n enabled but could not connect"
                    " - check URL and token in Settings"
                )
            elif err.name == "home_assistant":
                error_messages.append(
                    "Home Assistant enabled but could not connect"
                    " - check URL and token in Settings"
                )
            else:
                error_messages.append(f"MCP server '{err.name}' failed to connect: {err.error}")

        # Send error to frontend via data channel
        import json as json_module
        payload = json_module.dumps({
            "type": "mcp_error",
            "errors": error_messages,
        })
        try:
            await ctx.room.local_participant.publish_data(
                payload.encode("utf-8"),
                reliable=True,
                topic="mcp_error",
            )
        except Exception as e:
            logger.error(f"Failed to send MCP error to frontend: {e}")

    # Discover n8n workflows (n8n uses webhook-based execution, not MCP tools)
    n8n_workflow_tools = []
    n8n_workflow_name_map = {}
    n8n_base_url = None
    n8n_mcp = mcp_servers.get("n8n")
    if n8n_mcp:
        try:
            # Extract base URL from n8n MCP server config
            n8n_config = next((c for c in mcp_configs if c.name == "n8n"), None)
            if n8n_config:
                n8n_base_url = mcp_url_to_base_url(n8n_config.url)

            n8n_workflow_tools, n8n_workflow_name_map = await discover_n8n_workflows(
                n8n_mcp, n8n_base_url
            )
        except Exception as e:
            logger.error(f"Failed to discover n8n workflows: {e}")

    # Get runtime settings (from settings.json with .env fallback)
    runtime = get_runtime_settings()

    # Set GROQ_API_KEY env var for plugins that read from environment
    if runtime.get("groq_api_key"):
        os.environ["GROQ_API_KEY"] = runtime["groq_api_key"]

    memory_config = MemoryGuardConfig.from_env()
    lmstudio_base_url = (
        runtime["openai_base_url"]
        if runtime["llm_provider"] == "openai_compatible"
        else None
    )

    # Create CAALLLM instance (provider-agnostic wrapper)
    caal_llm = CAALLLM.from_settings(runtime)

    language = runtime["language"]

    # Log configuration
    logger.info("=" * 60)
    logger.info("STARTING VOICE AGENT")
    logger.info("=" * 60)
    logger.info(f"  Language: {language}")
    if runtime["stt_provider"] == "groq":
        logger.info(f"  STT: Groq (whisper-large-v3-turbo, lang={language})")
    else:
        logger.info(f"  STT: {SPEACHES_URL} ({WHISPER_MODEL}, lang={language})")
    if runtime["tts_provider"] == "piper":
        logger.info(f"  TTS: Piper ({runtime['tts_voice_piper']})")
    else:
        logger.info(f"  TTS: Kokoro ({runtime['tts_voice_kokoro']})")
    llm_provider = runtime["llm_provider"]
    if llm_provider == "ollama":
        logger.info(
            f"  LLM: Ollama ({runtime['ollama_model']}, "
            f"think={runtime['think']}, num_ctx={runtime['num_ctx']})"
        )
    elif llm_provider == "groq":
        logger.info(f"  LLM: Groq ({runtime['groq_model']})")
    elif llm_provider == "openai_compatible":
        model = runtime.get("openai_model", "?")
        url = runtime.get("openai_base_url", "?")
        logger.info(f"  LLM: OpenAI-compatible ({model}, {url})")
    elif llm_provider == "openrouter":
        logger.info(
            f"  LLM: OpenRouter ({runtime.get('openrouter_model', '?')})"
        )
    logger.info(f"  MCP: {list(mcp_servers.keys()) or 'None'}")
    logger.info(
        f"  Turn detection: interruptions={runtime['allow_interruptions']}, "
        f"endpointing_delay={runtime['min_endpointing_delay']}s"
    )
    logger.info("=" * 60)

    # Build STT - Speaches (local) or Groq (cloud)
    if runtime["stt_provider"] == "groq":
        base_stt = groq_plugin.STT(
            model="whisper-large-v3-turbo",
            language=language,
        )
    else:
        base_stt = openai.STT(
            base_url=f"{SPEACHES_URL}/v1",
            api_key="not-needed",  # Speaches doesn't require auth
            model=WHISPER_MODEL,
            language=language,
        )

    # Load wake word settings
    all_settings = settings_module.load_settings()
    wake_word_enabled = all_settings.get("wake_word_enabled", False)
    session_vad = silero.VAD.load(min_silence_duration=1.0)

    # Session reference for wake word callback (set after session creation)
    _session_ref: AgentSession | None = None

    if wake_word_enabled:
        import json

        wake_word_model = all_settings.get("wake_word_model", "models/hey_jarvis.onnx")
        wake_word_threshold = all_settings.get("wake_word_threshold", 0.5)
        wake_word_timeout = all_settings.get("wake_word_timeout", 3.0)
        wake_greetings = get_wake_greetings(language)

        async def on_wake_detected():
            """Play wake greeting directly via TTS, bypassing agent turn-taking."""
            nonlocal _session_ref
            if _session_ref is None:
                logger.warning("Wake detected but session not ready yet")
                return

            try:
                # Pick a random greeting
                greeting = random.choice(wake_greetings)
                logger.info(f"Wake word detected, playing greeting: {greeting}")

                # Get TTS and audio output from session
                tts = _session_ref.tts
                audio_output = _session_ref.output.audio

                # Synthesize and push audio frames directly (bypasses turn-taking)
                audio_stream = tts.synthesize(greeting)
                async for event in audio_stream:
                    if hasattr(event, "frame") and event.frame:
                        await audio_output.capture_frame(event.frame)

                # Flush to complete the audio segment
                audio_output.flush()

            except Exception as e:
                logger.warning(f"Failed to play wake greeting: {e}")

        async def on_state_changed(state):
            """Publish wake word state to connected clients."""
            payload = json.dumps({
                "type": "wakeword_state",
                "state": state.value,
            })
            try:
                await ctx.room.local_participant.publish_data(
                    payload.encode("utf-8"),
                    reliable=True,
                    topic="wakeword_state",
                )
                logger.debug(f"Published wake word state: {state.value}")
            except Exception as e:
                logger.warning(f"Failed to publish wake word state: {e}")

        stt_instance = WakeWordGatedSTT(
            inner_stt=base_stt,
            model_path=wake_word_model,
            threshold=wake_word_threshold,
            silence_timeout=wake_word_timeout,
            on_wake_detected=on_wake_detected,
            on_state_changed=on_state_changed,
        )
        logger.info(
            f"  Wake word: ENABLED (model={wake_word_model}, "
            f"threshold={wake_word_threshold})"
        )
    else:
        if runtime["stt_provider"] == "speaches":
            stt_instance = PreviewStreamAdapter(
                inner_stt=base_stt,
                vad_instance=session_vad,
            )
        else:
            stt_instance = base_stt
        logger.info("  Wake word: disabled")

    # Create TTS instance based on provider
    tts_provider = runtime["tts_provider"]

    # Kokoro speaks the languages it ships a trained voice for; only the rest
    # need Piper. Falling back for every non-English language would strand
    # French, Italian and Portuguese on Piper needlessly.
    if tts_provider == "kokoro" and not settings_module.kokoro_supports(language):
        # PIPER_URL defaults to SPEACHES_URL; if a dedicated Piper service is configured
        # (PIPER_URL != KOKORO_URL), Piper is available
        if PIPER_URL != KOKORO_URL:
            logger.info(
                f"Kokoro has no {language} voice, auto-switching to Piper"
            )
            tts_provider = "piper"
        else:
            logger.info(
                f"Kokoro TTS with {language} (no Piper service available)"
            )

    if tts_provider == "piper":
        piper_voice = runtime["tts_voice_piper"]
        tts_instance = openai.TTS(
            base_url=f"{PIPER_URL}/v1",
            api_key="not-needed",
            model=piper_voice,
            voice="default",  # Ignored by Piper but required by API
        )
    else:
        # Kokoro uses separate model and voice params
        # Using SyncOpenAITTS to bypass httpx async issues in LiveKit subprocess
        # The voice name carries the language (ff_siwis is French), so a voice
        # left over from another language would speak this one with the wrong
        # phonemes. Fall back to the language's default when they disagree.
        kokoro_voice = runtime["tts_voice_kokoro"]
        expected_prefix = settings_module.KOKORO_VOICE_MAP.get(language, "")[:1]
        if expected_prefix and not kokoro_voice.startswith(expected_prefix):
            # English accepts both American ("a") and British ("b") voices.
            if not (language == "en" and kokoro_voice[:1] in {"a", "b"}):
                kokoro_voice = settings_module.KOKORO_VOICE_MAP[language]
                logger.info(
                    f"Kokoro voice {runtime['tts_voice_kokoro']!r} is not a "
                    f"{language} voice, using {kokoro_voice!r}"
                )
        logger.info(f"  TTS voice: Kokoro {kokoro_voice} (lang={language})")
        tts_instance = SyncOpenAITTS(
            base_url=f"{KOKORO_URL}/v1",
            model=TTS_MODEL,
            voice=kokoro_voice,
            response_format="wav",
        )

    # Create session with STT and TTS (both OpenAI-compatible)
    logger.info(f"  STT instance type: {type(stt_instance).__name__}")
    logger.info(f"  STT capabilities: streaming={stt_instance.capabilities.streaming}")
    session = AgentSession(
        stt=stt_instance,
        llm=caal_llm,
        tts=tts_instance,
        vad=session_vad,
        allow_interruptions=runtime["allow_interruptions"],
        min_endpointing_delay=runtime["min_endpointing_delay"],
    )
    logger.info(f"  Session STT: {type(session.stt).__name__}")

    # Snapshot the selected conversation for this agent job. Switching the
    # active conversation reconnects the room, so a job never changes history
    # underneath an in-flight response.
    conversation_store = ConversationStore()
    conversation_id = conversation_store.ensure_active()
    conversation_summary, summary_through_rowid = conversation_store.get_summary(
        conversation_id
    )
    conversation_window = conversation_store.rehydration_window(conversation_id)
    logger.info(
        "Rehydrating %s post-summary messages "
        "(checkpoint=%s, truncated=%s, recall=%s)",
        len(conversation_window.messages),
        summary_through_rowid,
        conversation_window.truncated,
        bool(conversation_summary),
    )
    chat_ctx = llm.ChatContext.empty()
    for saved_message in conversation_window.messages:
        chat_ctx.add_message(
            role=saved_message["role"],
            content=saved_message["content"],
        )

    # Set session reference for wake word callback
    _session_ref = session

    # ==========================================================================
    # Round-trip latency tracking
    # ==========================================================================

    _transcription_time: float | None = None
    inference_lock = asyncio.Lock()
    session_prefix_warm_task: asyncio.Task | None = None
    rolling_summary_task: asyncio.Task | None = None

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev) -> None:
        nonlocal _transcription_time, rolling_summary_task
        if ev.is_final:
            _transcription_time = time.perf_counter()
            if rolling_summary_task and not rolling_summary_task.done():
                logger.info("Cancelling rolling summary for a live user turn")
                rolling_summary_task.cancel()
            logger.debug(f"User said: {ev.transcript[:80]}...")
        else:
            logger.debug(f"User partial: {ev.transcript[:80]}...")

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev) -> None:
        nonlocal _transcription_time
        if ev.new_state == "speaking" and _transcription_time is not None:
            latency_ms = (time.perf_counter() - _transcription_time) * 1000
            logger.info(f"ROUND-TRIP LATENCY: {latency_ms:.0f}ms (LLM + TTS)")
            _transcription_time = None

        # Notify wake word STT of agent state for silence timer management
        if isinstance(stt_instance, WakeWordGatedSTT):
            stt_instance.set_agent_busy(ev.new_state in ("thinking", "speaking"))

    _tool_activity_id: str | None = None

    async def _publish_tool_status(
        tool_used: bool,
        tool_names: list[str],
        tool_params: list[dict],
        status: str = "running",
    ) -> None:
        """Publish tool usage status to frontend via data packet."""
        import json
        nonlocal _tool_activity_id

        if tool_used and status == "running" and _tool_activity_id is None:
            _tool_activity_id = f"tool_{uuid.uuid4().hex}"
        activity_id = _tool_activity_id
        payload = json.dumps({
            "id": activity_id,
            "tool_used": tool_used,
            "tool_names": tool_names,
            "tool_params": tool_params,
            "status": status if tool_used else "idle",
            "timestamp": time.time() * 1000,
        })

        try:
            await ctx.room.local_participant.publish_data(
                payload.encode("utf-8"),
                reliable=True,
                topic="tool_status",
            )
            logger.debug(f"Published tool status: used={tool_used}, names={tool_names}")
            if tool_used and status in {"complete", "failed"} and activity_id:
                display_names = ", ".join(name.replace("_", " ") for name in tool_names)
                conversation_store.append_message(
                    conversation_id,
                    role="tool",
                    content=(
                        f"Used {display_names}"
                        if status == "complete"
                        else f"Failed to use {display_names}"
                    ),
                    message_id=activity_id,
                    metadata={
                        "tool_names": tool_names,
                        "tool_params": tool_params,
                        "status": status,
                    },
                )
                _tool_activity_id = None
        except Exception as e:
            logger.warning(f"Failed to publish tool status: {e}")

    # ==========================================================================

    # Create HASS tools only if Home Assistant is connected
    hass_tool_definitions = []
    hass_tool_callables = {}
    hass_server = mcp_servers.get("home_assistant")
    if hass_server:
        # Detect tool prefix (some HA MCP servers use 'assist__' prefix)
        hass_tool_prefix = await detect_hass_tool_prefix(hass_server)
        if hass_tool_prefix:
            logger.info(f"Home Assistant MCP uses '{hass_tool_prefix}' prefix")
        hass_tool_definitions, hass_tool_callables = create_hass_tools(
            hass_server, tool_prefix=hass_tool_prefix
        )
        logger.info("Home Assistant tools enabled: hass")

    # Initialize short-term memory (singleton, persists across restarts)
    short_term_memory = ShortTermMemory()
    memory_count = len(short_term_memory.list_keys())
    if memory_count > 0:
        logger.info(f"Short-term memory loaded: {memory_count} entries")
    else:
        logger.info("Short-term memory initialized (empty)")

    session_context = build_session_context(
        settings_module.load_session_date_context(
            timezone_id=TIMEZONE_ID,
            timezone_display=TIMEZONE_DISPLAY,
            language=language,
        ),
        user_memory=short_term_memory.get_context_message(),
        conversation_recall=recall_block(conversation_summary),
    )

    # Create agent with CAALLLM and all MCP servers
    assistant = VoiceAssistant(
        caal_llm=caal_llm,
        language=language,
        mcp_servers=mcp_servers,
        n8n_workflow_tools=n8n_workflow_tools,
        n8n_workflow_name_map=n8n_workflow_name_map,
        n8n_base_url=n8n_base_url,
        on_tool_status=_publish_tool_status,
        tool_cache_size=runtime["tool_cache_size"],
        max_turns=runtime["max_turns"],
        hass_tool_definitions=hass_tool_definitions,
        hass_tool_callables=hass_tool_callables,
        short_term_memory=short_term_memory,
        chat_ctx=chat_ctx,
        session_context=session_context,
        inference_lock=inference_lock,
    )
    call_tools = await discover_tools(assistant, caal_llm.provider_instance)

    async def _publish_conversation_updated() -> None:
        import json

        try:
            await ctx.room.local_participant.publish_data(
                json.dumps({"conversation_id": conversation_id}).encode("utf-8"),
                reliable=True,
                topic="conversation_updated",
            )
        except Exception as e:
            logger.debug(f"Failed to publish conversation update: {e}")

    rolling_summary_next_attempt = 0.0

    async def _maybe_update_rolling_summary() -> None:
        """Checkpoint a large active-call delta without competing with a turn."""
        nonlocal rolling_summary_next_attempt
        trigger_chars = int(
            os.getenv("HISTORY_ROLLING_SUMMARY_TRIGGER_CHARS", "4000")
        )
        if trigger_chars <= 0:
            return
        _, pending_chars = await asyncio.to_thread(
            conversation_store.unsummarized_stats, conversation_id
        )
        if pending_chars < trigger_chars:
            return

        now = time.monotonic()
        if now < rolling_summary_next_attempt:
            return
        retry_seconds = float(
            os.getenv("HISTORY_ROLLING_SUMMARY_RETRY_SECONDS", "120")
        )
        rolling_summary_next_attempt = now + max(0, retry_seconds)
        timeout = float(os.getenv("HISTORY_SUMMARY_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            return

        logger.info(
            "Rolling summary triggered with %s unsummarized characters",
            pending_chars,
        )
        try:
            async with inference_lock:
                await asyncio.wait_for(
                    checkpoint_conversation_summary(
                        conversation_store,
                        conversation_id,
                        caal_llm.provider_instance,
                    ),
                    timeout=timeout,
                )
        except asyncio.CancelledError:
            rolling_summary_next_attempt = 0.0
            raise
        except TimeoutError:
            logger.warning("Rolling conversation summary timed out after %.1fs", timeout)
        except Exception as error:
            logger.warning("Rolling conversation summary failed: %s", error)

    @session.on("conversation_item_added")
    def on_conversation_item_added(ev) -> None:
        nonlocal rolling_summary_task
        item = ev.item
        if not isinstance(item, llm.ChatMessage) or item.role not in {"user", "assistant"}:
            return
        text = item.text_content
        if not text:
            return
        try:
            conversation_store.append_message(
                conversation_id,
                role=item.role,
                content=text,
                message_id=item.id,
                # Assistant items are created before tool execution begins. Save
                # them when finalized so restored history keeps tool rows between
                # the user's request and the assistant's answer.
                created_at=item.created_at if item.role == "user" else time.time(),
                metadata={"interrupted": item.interrupted},
            )
            asyncio.create_task(_publish_conversation_updated())
            if item.role == "assistant" and (
                rolling_summary_task is None or rolling_summary_task.done()
            ):
                rolling_summary_task = asyncio.create_task(
                    _maybe_update_rolling_summary()
                )
        except Exception as e:
            logger.warning(f"Failed to persist conversation item: {e}")

    # Create event to wait for session close (BEFORE session.start to avoid race condition)
    close_event = asyncio.Event()

    @session.on("close")
    def on_session_close(ev) -> None:
        logger.info(f"Session closed: {ev.reason}")
        close_event.set()

    # ==========================================================================
    # Webhook Command Handler (via LiveKit data channel)
    # ==========================================================================

    async def _handle_webhook_command(data: rtc.DataPacket) -> None:
        """Handle commands from webhook server via LiveKit data channel."""
        nonlocal call_tools
        if data.topic != "webhook_command":
            return

        try:
            import json

            cmd = json.loads(data.data.decode("utf-8"))
            action = cmd.get("action")
            logger.info(f"Received webhook command: {action}")

            if action == "announce":
                message = cmd.get("message", "")
                if message:
                    await session.say(message)

            elif action == "wake":
                lang = settings_module.get_setting("language", "en")
                greetings = get_wake_greetings(lang)
                greeting = random.choice(greetings)
                await session.say(greeting)

            elif action == "reload_tools":
                # Clear agent's internal caches
                assistant._llm_tools_cache = None

                # Clear n8n module-level cache so fresh notes are fetched
                from caal.integrations.n8n import clear_caches as clear_n8n_caches
                clear_n8n_caches()

                # Re-discover n8n workflows if MCP is available
                n8n_mcp = assistant._caal_mcp_servers.get("n8n")
                if n8n_mcp and assistant._n8n_base_url:
                    try:
                        tools, name_map = await discover_n8n_workflows(
                            n8n_mcp, assistant._n8n_base_url
                        )
                        assistant._n8n_workflow_tools = tools
                        assistant._n8n_workflow_name_map = name_map
                        logger.info(f"Reloaded {len(tools)} n8n workflows")
                    except Exception as e:
                        logger.error(f"Failed to re-discover n8n workflows: {e}")

                # Announce if requested
                if msg := cmd.get("message"):
                    await session.say(msg)
                elif tool_name := cmd.get("tool_name"):
                    await session.say(f"A new tool called '{tool_name}' is now available.")

                call_tools = await discover_tools(
                    assistant, caal_llm.provider_instance
                )

        except Exception as e:
            logger.error(f"Failed to process webhook command: {e}")

    @ctx.room.on("data_received")
    def on_data_received(data: rtc.DataPacket) -> None:
        """Sync wrapper for async webhook command handler."""
        asyncio.create_task(_handle_webhook_command(data))

    # Start session AFTER handlers are registered
    await session.start(
        room=ctx.room,
        agent=assistant,
    )

    async def _warm_session_prefix() -> None:
        async with inference_lock:
            warmed = await warm_prompt_prefix(
                caal_llm.provider_instance,
                stable_prompt=assistant._stable_prompt,
                tools=call_tools,
                session_context=session_context,
                history=conversation_window.messages,
            )
        logger.info("Session prefix warm-up %s", "complete" if warmed else "skipped")
        try:
            conversation_store.append_message(
                conversation_id,
                role="tool",
                content=(
                    "Session cache ready"
                    if warmed
                    else "Session cache warm-up did not complete"
                ),
                message_id=f"session_cache_{uuid.uuid4().hex}",
                metadata={
                    "kind": "session_cache",
                    "status": "complete" if warmed else "failed",
                },
            )
            await _publish_conversation_updated()
        except Exception as error:
            logger.warning("Could not record session-cache status: %s", error)

    # The canned greeting masks most prefill latency. If the user barges in,
    # VoiceAssistant.llm_node awaits this same task before starting real work.
    session_prefix_warm_task = asyncio.create_task(_warm_session_prefix())
    assistant._session_prefix_warm_task = session_prefix_warm_task

    # Say a canned greeting using agent name — avoids LLM call that could trigger tools
    agent_name = settings_module.get_setting("agent_name", "Cal")
    await session.say(
        f"Hello! I'm {agent_name}, your voice assistant. How can I help you?",
        add_to_chat_ctx=False,
    )

    logger.info("Agent ready - listening for speech...")

    # ==========================================================================
    # Memory guard
    # ==========================================================================
    # A live call's KV cache is bounded by nothing - it grows with the
    # conversation until the machine swaps. Rather than truncate context
    # mid-call (which would quietly change what the assistant remembers), end
    # the call and let the next one start fresh from rehydrated history.

    memory_trip: MemoryTrip | None = None
    hard_reset_ready: bool | None = None

    async def _reload_model(reason: str) -> bool:
        if not lmstudio_base_url:
            logger.warning("Cannot reload model for memory trip: no LM Studio endpoint")
            return False
        logger.warning("Reloading LM Studio model for memory: %s", reason)
        return await asyncio.to_thread(
            reload_local_lmstudio_model,
            lmstudio_base_url,
            runtime["openai_model"],
            model_key=os.getenv("CAAL_LMSTUDIO_MODEL"),
            lms_bin=os.getenv("CAAL_LMS_BIN"),
            context_length=int(os.getenv("CAAL_LMSTUDIO_CONTEXT_LENGTH", "32768")),
            parallel=int(os.getenv("CAAL_LMSTUDIO_PARALLEL", "4")),
        )

    async def _publish_memory_reconnect(trip: MemoryTrip) -> None:
        """Tell the desktop to replace this job without changing conversation."""
        import json

        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(
                    {
                        "type": "memory_guard_trip",
                        "conversation_id": conversation_id,
                        "reason": trip.reason,
                    }
                ).encode("utf-8"),
                reliable=True,
                topic="memory_guard_trip",
            )
        except Exception as error:
            logger.warning("Could not request reconnect after memory trip: %s", error)

    async def _record_memory_trip(trip: MemoryTrip) -> None:
        try:
            conversation_store.append_message(
                conversation_id,
                role="tool",
                content=f"Session ended - {trip.reason}",
                message_id=f"memory_guard_{uuid.uuid4().hex}",
                metadata={
                    "kind": "memory_guard",
                    "reason": trip.reason,
                    "status": "failed",
                },
            )
            await _publish_conversation_updated()
        except Exception as error:
            logger.warning("Could not record memory shutdown notice: %s", error)

    async def _run_post_clearance_maintenance() -> None:
        """Checkpoint durable history, then leave the production prefix warm."""
        summary_timeout = float(os.getenv("HISTORY_SUMMARY_TIMEOUT_SECONDS", "30"))
        if summary_timeout > 0:
            try:
                async with inference_lock:
                    await asyncio.wait_for(
                        checkpoint_conversation_summary(
                            conversation_store,
                            conversation_id,
                            caal_llm.provider_instance,
                        ),
                        timeout=summary_timeout,
                    )
            except TimeoutError:
                logger.warning(
                    "Post-clearance conversation summary timed out after %.1fs",
                    summary_timeout,
                )
            except Exception as error:
                logger.warning("Post-clearance conversation summary failed: %s", error)

        current_tools = await discover_tools(assistant, caal_llm.provider_instance)
        async with inference_lock:
            warmed = await warm_prompt_prefix(
                caal_llm.provider_instance,
                stable_prompt=assistant._stable_prompt,
                tools=current_tools,
            )
        logger.info("Stable prefix rehydration %s", "complete" if warmed else "skipped")

    async def _end_call_for_memory(trip: MemoryTrip) -> None:
        """Terminate this session on a memory trip.

        Marking the agent terminated prevents new turns while LM Studio finishes
        or aborts the in-flight request during session teardown.
        """
        nonlocal hard_reset_ready, memory_trip
        memory_trip = trip
        logger.warning("Memory guard tripped, ending session: %s", trip.reason)

        # Mark terminated so llm_node refuses any further turn.
        assistant._memory_terminated = trip.reason

        # At critical system pressure there may not be enough memory left to
        # close LiveKit, clear caches, and dispatch a replacement job. Reclaim
        # the model first; breaking an in-flight request is intentional here.
        if trip.restart_first:
            hard_reset_ready = await _reload_model(trip.reason)

        # Surface it as system text in the transcript rather than speech. The
        # "tool" role is the transcript's system-activity lane and is already
        # excluded from context and summary queries, so this is display-only and
        # never reaches the model. Speaking it would also mean holding the call
        # open for several seconds of TTS purely to narrate its own ending.
        await _record_memory_trip(trip)

        # End the session the same way a hang-up does, so both paths run the
        # same teardown: closing it fires the "close" handler, which sets
        # close_event. Setting the event directly would leave the session open
        # and skip that handler, making a tripped call tear down differently
        # from every other call for no reason.
        try:
            await asyncio.wait_for(session.aclose(), timeout=10)
        except Exception as e:
            logger.warning(f"Could not close session cleanly: {e}")
            close_event.set()

    memory_guard_task = asyncio.create_task(
        guard_loop(_end_call_for_memory, memory_config)
    )

    # Wait until session closes (room disconnects, memory pressure, etc.)
    try:
        await close_event.wait()
    finally:
        memory_guard_task.cancel()
        await asyncio.gather(memory_guard_task, return_exceptions=True)
        if session_prefix_warm_task and not session_prefix_warm_task.done():
            session_prefix_warm_task.cancel()
        if session_prefix_warm_task:
            await asyncio.gather(session_prefix_warm_task, return_exceptions=True)
        if rolling_summary_task and not rolling_summary_task.done():
            rolling_summary_task.cancel()
        if rolling_summary_task:
            await asyncio.gather(rolling_summary_task, return_exceptions=True)
        speech_cache_urls: set[str] = set()
        if runtime["stt_provider"] == "speaches":
            speech_cache_urls.add(SPEACHES_URL)
        if tts_provider == "kokoro":
            speech_cache_urls.add(KOKORO_URL)
        for speech_url in speech_cache_urls:
            await asyncio.to_thread(clear_local_speech_cache, speech_url)

        clearance_ready = True
        if memory_trip:
            if memory_trip.restart_first:
                clearance_ready = hard_reset_ready is True
            else:
                recovered = await wait_for_recovery(memory_config)
                if not recovered:
                    clearance_ready = await _reload_model(
                        "normal request teardown did not reclaim the model's memory"
                    )

        if clearance_ready:
            await _run_post_clearance_maintenance()
            if memory_trip:
                await _publish_memory_reconnect(memory_trip)
        else:
            logger.error(
                "Session memory was cleared, but Qwen did not become ready; "
                "leaving the call disconnected"
            )
    # Returning from the entrypoint does not finalize a LiveKit job. Explicitly
    # shut it down so its agent participant leaves the room before the desktop
    # reconnects for a different conversation. Otherwise the stale participant
    # prevents a replacement job from being dispatched and receives chat streams
    # without an AgentSession callback.
    ctx.shutdown("agent session closed")


# =============================================================================
# Model Preloading
# =============================================================================


def preload_models():
    """Preload STT and LLM models on startup.

    Ensures models are ready before first user connection, avoiding
    delays on first request (especially important on HDDs).

    Skips preloading entirely if wizard not complete (no provider selected yet).
    Skips individual preloads when using cloud providers (Groq).
    Note: Kokoro (remsky/kokoro-fastapi) preloads its own models at startup.
    """
    settings = settings_module.load_settings()

    # Skip all preloading if wizard not complete
    if not settings.get("first_launch_completed", False):
        logger.info("Skipping model preload (wizard not complete)")
        return

    stt_provider = settings.get("stt_provider", "speaches")
    llm_provider = settings.get("llm_provider", "ollama")

    logger.info("Preloading models...")

    # Download Whisper STT model (skip if using Groq cloud STT)
    if stt_provider == "groq":
        logger.info("  Skipping STT preload (using Groq)")
    else:
        speaches_url = os.getenv("SPEACHES_URL", "http://speaches:8000")
        whisper_model = os.getenv("WHISPER_MODEL", "Systran/faster-whisper-medium")
        try:
            logger.info(f"  Loading STT: {whisper_model}")
            response = requests.post(
                f"{speaches_url}/v1/models/{whisper_model}",
                timeout=300
            )
            if response.status_code == 404:
                response = requests.post(
                    f"{speaches_url}/v1/models?model_name={whisper_model}",
                    timeout=300
                )
            if response.status_code == 200:
                logger.info("  ✓ STT ready")
            else:
                logger.warning(f"  STT model download returned {response.status_code}")
        except Exception as e:
            logger.warning(f"  Failed to preload STT model: {e}")

    # Warm up the selected LLM provider.
    if llm_provider == "groq":
        logger.info("  Skipping LLM preload (using Groq)")
    elif llm_provider == "openai_compatible":
        runtime = get_runtime_settings()

        async def _warm_openai_service_prefix() -> bool:
            preload_llm = CAALLLM.from_settings(runtime)
            preload_memory = ShortTermMemory()
            preload_memory.reload()
            tool_context = ToolContext(
                mcp_configs=load_mcp_config(),
                short_term_memory=preload_memory,
                provider=preload_llm.provider_instance,
            )
            try:
                await tool_context.ensure_mcp_initialized()
                tools = await discover_tools(
                    tool_context, preload_llm.provider_instance
                )
                return await warm_prompt_prefix(
                    preload_llm.provider_instance,
                    stable_prompt=load_prompt(runtime.get("language", "en")),
                    tools=tools,
                )
            finally:
                await asyncio.gather(
                    *(
                        server.aclose()
                        for server in tool_context._caal_mcp_servers.values()
                    ),
                    return_exceptions=True,
                )

        try:
            logger.info(
                "  Warming OpenAI-compatible model/runtime and exact tool prefix"
            )
            warmed = asyncio.run(_warm_openai_service_prefix())
            if warmed:
                logger.info(
                    "  ✓ LLM model, runtime, kernels, and stable prompt ready"
                )
            else:
                logger.warning("  Stable LLM prefix warm-up did not complete")
        except Exception as e:
            logger.warning(f"  Failed to warm OpenAI-compatible LLM: {e}")
    else:
        ollama_host = settings.get("ollama_host") or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        ollama_model = settings.get("ollama_model") or os.getenv("OLLAMA_MODEL", "ministral-3:8b")
        ollama_num_ctx = settings.get("num_ctx", int(os.getenv("OLLAMA_NUM_CTX", "8192")))
        try:
            logger.info(f"  Loading LLM: {ollama_model} (num_ctx={ollama_num_ctx})")
            response = requests.post(
                f"{ollama_host}/api/generate",
                json={
                    "model": ollama_model,
                    "prompt": "hi",
                    "stream": False,
                    "keep_alive": -1,
                    "options": {"num_ctx": ollama_num_ctx}
                },
                timeout=180
            )
            if response.status_code == 200:
                logger.info("  ✓ LLM ready")
            else:
                logger.warning(f"  LLM warmup returned {response.status_code}")
        except Exception as e:
            logger.warning(f"  Failed to preload LLM: {e}")


# =============================================================================
# Webhook Server (runs in background thread)
# =============================================================================

WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8889"))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")


def run_webhook_server_sync():
    """Run webhook server in a separate thread (blocking).

    This starts the webhook server immediately on agent startup,
    so /setup/status and other endpoints are available before
    any user connects.
    """
    import uvicorn

    from caal.webhooks import app

    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="warning",
        log_config=None,  # Don't configure logging (prevents duplicate handlers in forked workers)
    )
    server = uvicorn.Server(config)
    logger.info(f"Starting webhook server on port {WEBHOOK_PORT}")
    server.run()


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import threading

    # Start webhook server in background thread (available immediately)
    webhook_thread = threading.Thread(target=run_webhook_server_sync, daemon=True)
    webhook_thread.start()

    # Preload models before starting worker
    preload_models()

    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            # Suppress memory warnings (models use ~1GB, this is expected)
            job_memory_warn_mb=0,
        )
    )
