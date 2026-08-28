"""Prompt-prefix composition and best-effort model prompt-cache warm-up."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm.providers import LLMProvider

logger = logging.getLogger(__name__)

STABLE_BUNDLE_VERSION = 1


@dataclass(frozen=True)
class StablePromptBundle:
    """Versioned, model-visible prefix shared by every ordinary call."""

    stable_prompt: str
    tools: list[dict[str, Any]]
    toolset_fingerprint: str
    language: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = STABLE_BUNDLE_VERSION
    created_at: float = field(default_factory=time.time)


def canonicalize_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return deterministically ordered JSON-compatible tool schemas."""
    named: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for tool in tools or []:
        normalized = json.loads(json.dumps(tool, ensure_ascii=False, sort_keys=True))
        function = normalized.get("function") if isinstance(normalized, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            named[name] = normalized
        else:
            unnamed.append(normalized)
    return [
        *(named[name] for name in sorted(named)),
        *sorted(unnamed, key=_canonical_json),
    ]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def toolset_fingerprint(tools: list[dict[str, Any]] | None) -> str:
    canonical = canonicalize_tools(tools)
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def build_stable_prompt_bundle(
    *,
    stable_prompt: str,
    tools: list[dict[str, Any]] | None,
    language: str,
    model: str,
    metadata: dict[str, Any] | None = None,
) -> StablePromptBundle:
    canonical_tools = canonicalize_tools(tools)
    return StablePromptBundle(
        stable_prompt=stable_prompt,
        tools=canonical_tools,
        toolset_fingerprint=toolset_fingerprint(canonical_tools),
        language=language,
        model=model,
        metadata=metadata or {},
    )


def stable_bundle_path() -> Path:
    configured = os.getenv("CAAL_STABLE_PROMPT_BUNDLE")
    if configured:
        return Path(configured)
    return Path(os.getenv("CAAL_MEMORY_DIR", "data")) / "stable-prompt-bundle.json"


def save_stable_prompt_bundle(
    bundle: StablePromptBundle,
    path: str | Path | None = None,
) -> Path:
    """Atomically replace the stable bundle consumed by future jobs."""
    destination = Path(path) if path else stable_bundle_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(bundle), handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_stable_prompt_bundle(
    *,
    language: str,
    model: str,
    stable_prompt: str,
    path: str | Path | None = None,
) -> StablePromptBundle:
    """Load and validate the exact startup-generated stable prefix."""
    source = Path(path) if path else stable_bundle_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        bundle = StablePromptBundle(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Stable prompt bundle is unavailable: {source}") from error

    if bundle.version != STABLE_BUNDLE_VERSION:
        raise RuntimeError(f"Unsupported stable prompt bundle version {bundle.version}")
    if bundle.language != language or bundle.model != model:
        raise RuntimeError("Stable prompt bundle does not match the active language/model")
    if bundle.stable_prompt != stable_prompt:
        raise RuntimeError("Stable prompt bundle does not match current system instructions")
    canonical_tools = canonicalize_tools(bundle.tools)
    if bundle.toolset_fingerprint != toolset_fingerprint(canonical_tools):
        raise RuntimeError("Stable prompt bundle tool fingerprint is invalid")
    return StablePromptBundle(
        stable_prompt=bundle.stable_prompt,
        tools=canonical_tools,
        toolset_fingerprint=bundle.toolset_fingerprint,
        language=bundle.language,
        model=bundle.model,
        metadata=bundle.metadata,
        version=bundle.version,
        created_at=bundle.created_at,
    )


def build_session_context(
    date_context: str,
    *,
    user_memory: str | None = None,
    conversation_recall: str | None = None,
) -> str:
    """Freeze the variable context that remains stable for one connected call."""
    sections = ["# Current Session", date_context.strip()]
    if user_memory:
        sections.append(user_memory.strip())
    if conversation_recall:
        sections.append(conversation_recall.strip())
    return "\n\n".join(section for section in sections if section)


def compose_system_prompt(stable_prompt: str, session_context: str | None = None) -> str:
    """Append variable call context after every stable system-prompt token."""
    if not session_context:
        return stable_prompt
    return f"{stable_prompt}\n\n{session_context}"


async def warm_prompt_prefix(
    provider: LLMProvider,
    *,
    stable_prompt: str,
    tools: list[dict[str, Any]] | None,
    session_context: str | None = None,
    history: list[dict[str, Any]] | None = None,
) -> bool:
    """Run one bounded inference whose prefix matches the next production call."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": compose_system_prompt(stable_prompt, session_context),
        }
    ]
    messages.extend(
        {"role": message["role"], "content": message["content"]}
        for message in (history or [])
    )
    messages.append(
        {
            "role": "user",
            "content": "Reply with OK to confirm that this context is ready.",
        }
    )
    timeout = float(os.getenv("CAAL_PREFIX_WARM_TIMEOUT", "30"))
    max_tokens = int(os.getenv("CAAL_PREFIX_WARM_TOKENS", "1"))
    if timeout <= 0 or max_tokens <= 0:
        return False

    try:
        await asyncio.wait_for(
            provider.chat(
                messages=messages,
                tools=tools,
                think=False,
                temperature=0,
                max_tokens=max_tokens,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Prompt-prefix warm-up timed out after %.1fs", timeout)
        return False
    except Exception as error:
        logger.warning("Prompt-prefix warm-up failed: %s", error)
        return False
    return True


async def require_prompt_prefix_ready(
    provider: LLMProvider,
    *,
    stable_prompt: str,
    tools: list[dict[str, Any]] | None,
    session_context: str | None = None,
    history: list[dict[str, Any]] | None = None,
    label: str = "Prompt prefix",
) -> None:
    """Warm an exact prefix or fail the preparation stage that requires it."""
    warmed = await warm_prompt_prefix(
        provider,
        stable_prompt=stable_prompt,
        tools=tools,
        session_context=session_context,
        history=history,
    )
    if not warmed:
        raise RuntimeError(f"{label} warm-up did not complete")
