"""Durable summaries checkpointed at bounded turn and session boundaries."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .conversations import ConversationStore

if TYPE_CHECKING:
    from .llm.providers import LLMProvider

logger = logging.getLogger(__name__)

SUMMARY_INPUT_MAX_CHARS = 6000
SUMMARY_OUTPUT_MAX_TOKENS = 384


def recall_block(summary: str | None) -> str:
    """Format hidden long-term conversation context for the system prompt."""
    if not summary:
        return ""
    return (
        "Earlier turns in this conversation that are no longer quoted verbatim "
        f"were summarized as follows:\n{summary}\n\n"
        "Use this only as background context. Do not announce or mention the "
        "summary itself."
    )


async def checkpoint_conversation_summary(
    store: ConversationStore,
    conversation_id: str,
    provider: LLMProvider,
) -> str | None:
    """Summarize one bounded durable delta and atomically advance its checkpoint."""
    summary, summarized_through = store.get_summary(conversation_id)
    max_chars = int(
        os.getenv("HISTORY_SUMMARY_INPUT_MAX_CHARS", str(SUMMARY_INPUT_MAX_CHARS))
    )
    if max_chars <= 0:
        return summary

    pending = store.unsummarized_messages(
        conversation_id,
        after_rowid=summarized_through,
        max_chars=max_chars,
    )
    if not pending:
        return summary

    transcript = "\n".join(
        f"{message['role'].upper()}: {message['content']}" for message in pending
    )
    existing = summary or "(No earlier summary yet.)"
    max_tokens = int(
        os.getenv("HISTORY_SUMMARY_OUTPUT_TOKENS", str(SUMMARY_OUTPUT_MAX_TOKENS))
    )
    if max_tokens <= 0:
        return summary
    response = await provider.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "Maintain a compact, factual memory of one conversation. "
                    "Preserve user preferences, names, decisions, commitments, "
                    "important results, and unresolved tasks. Remove greetings, "
                    "filler, repetition, and obsolete details. Write plain text "
                    "under 250 words. Never add facts not present in the input."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Update the existing memory with the newly completed turns.\n\n"
                    f"EXISTING MEMORY:\n{existing}\n\n"
                    f"NEWLY COMPLETED TURNS:\n{transcript}"
                ),
            },
        ],
        tools=None,
        think=False,
        temperature=0,
        max_tokens=max_tokens,
    )
    updated = (response.content or "").strip()
    if not updated:
        logger.warning("Conversation summary model returned no text")
        return summary

    through_rowid = int(pending[-1]["rowid"])
    saved = store.save_summary(
        conversation_id,
        updated,
        through_rowid=through_rowid,
        expected_through_rowid=summarized_through,
    )
    if not saved:
        logger.info("Discarded a stale concurrent conversation summary")
        return store.get_summary(conversation_id)[0]
    logger.info("Updated conversation summary through message row %s", through_rowid)
    return updated
