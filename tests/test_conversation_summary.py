import asyncio

from livekit.agents import llm

from caal.conversation_summary import recall_block, refresh_conversation_summary
from caal.conversations import ConversationStore
from caal.llm.llm_node import _build_messages_from_context
from caal.llm.providers import LLMResponse


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return LLMResponse(
            content=f"Rolling memory version {len(self.calls)}",
            tool_calls=[],
        )


def test_refresh_conversation_summary_covers_truncated_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_REHYDRATE_MESSAGES", "2")
    monkeypatch.setenv("HISTORY_REHYDRATE_MAX_CHARS", "100")
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    for index in range(6):
        store.append_message(
            conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"turn {index}",
        )

    provider = FakeProvider()
    summary = asyncio.run(
        refresh_conversation_summary(store, conversation_id, provider)
    )

    assert summary == "Rolling memory version 1"
    stored_summary, through_rowid = store.get_summary(conversation_id)
    assert stored_summary == summary
    assert through_rowid > 0
    assert "turn 0" in provider.calls[0][1]["content"]
    assert "turn 3" in provider.calls[0][1]["content"]
    assert "turn 4" not in provider.calls[0][1]["content"]
    assert "Rolling memory version 1" in recall_block(summary)


def test_refresh_is_a_noop_until_history_is_truncated(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="Hello")
    provider = FakeProvider()

    assert asyncio.run(
        refresh_conversation_summary(store, conversation_id, provider)
    ) is None
    assert provider.calls == []


def test_recall_is_injected_into_the_existing_system_prompt():
    chat_context = llm.ChatContext.empty()
    chat_context.add_message(role="system", content="You are CAAL.")
    chat_context.add_message(role="user", content="Continue our conversation")

    messages = _build_messages_from_context(
        chat_context,
        conversation_recall="Earlier the user chose Taipei.",
    )

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == (
        "You are CAAL.\n\nEarlier the user chose Taipei."
    )
    assert len([message for message in messages if message["role"] == "system"]) == 1
