"""Prompt-prefix composition and best-effort LM Studio cache warm-up."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm.providers import LLMProvider

logger = logging.getLogger(__name__)


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
