"""Rolling summaries for transcript turns outside the rehydration window."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .conversations import ConversationStore

if TYPE_CHECKING:
    from .llm.providers import LLMProvider

logger = logging.getLogger(__name__)

SUMMARY_INPUT_MAX_CHARS = 6000


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


async def refresh_conversation_summary(
    store: ConversationStore,
    conversation_id: str,
    provider: LLMProvider,
) -> str | None:
    """Summarize every persisted turn that fell outside the replay window."""
    window = store.context_window(conversation_id)
    summary, summarized_through = store.get_summary(conversation_id)
    if not window.truncated or window.oldest_rowid is None:
        return summary

    max_chars = int(
        os.getenv("HISTORY_SUMMARY_INPUT_MAX_CHARS", str(SUMMARY_INPUT_MAX_CHARS))
    )
    if max_chars <= 0:
        return summary

    while True:
        pending = store.unsummarized_messages(
            conversation_id,
            after_rowid=summarized_through,
            before_rowid=window.oldest_rowid,
            max_chars=max_chars,
        )
        if not pending:
            return summary

        transcript = "\n".join(
            f"{message['role'].upper()}: {message['content']}" for message in pending
        )
        existing = summary or "(No earlier summary yet.)"
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
                        "Update the existing memory with the newly archived turns.\n\n"
                        f"EXISTING MEMORY:\n{existing}\n\n"
                        f"NEWLY ARCHIVED TURNS:\n{transcript}"
                    ),
                },
            ],
            tools=None,
            think=False,
        )
        updated = (response.content or "").strip()
        if not updated:
            logger.warning("Conversation summary model returned no text")
            return summary

        summarized_through = int(pending[-1]["rowid"])
        store.save_summary(
            conversation_id,
            updated,
            through_rowid=summarized_through,
        )
        summary = updated
        logger.info(
            "Updated conversation summary through message row %s",
            summarized_through,
        )
