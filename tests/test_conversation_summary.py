import asyncio

from livekit.agents import llm

from caal.conversation_summary import (
    checkpoint_conversation_summary,
    recall_block,
)
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


def test_boundary_summary_covers_all_completed_turns(tmp_path):
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
        checkpoint_conversation_summary(store, conversation_id, provider)
    )

    assert summary == "Rolling memory version 1"
    stored_summary, through_rowid = store.get_summary(conversation_id)
    assert stored_summary == summary
    assert through_rowid > 0
    assert "turn 0" in provider.calls[0][1]["content"]
    assert "turn 5" in provider.calls[0][1]["content"]
    assert "Rolling memory version 1" in recall_block(summary)


def test_boundary_summary_is_a_noop_without_new_messages(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    provider = FakeProvider()

    assert asyncio.run(
        checkpoint_conversation_summary(store, conversation_id, provider)
    ) is None
    assert provider.calls == []


def test_boundary_summary_uses_previous_checkpoint_and_new_delta(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="First call")
    provider = FakeProvider()

    asyncio.run(checkpoint_conversation_summary(store, conversation_id, provider))
    store.append_message(conversation_id, role="user", content="Second call")
    asyncio.run(checkpoint_conversation_summary(store, conversation_id, provider))

    second_prompt = provider.calls[1][1]["content"]
    assert "Rolling memory version 1" in second_prompt
    assert "Second call" in second_prompt
    assert "First call" not in second_prompt


def test_boundary_summary_excludes_interrupted_assistant_output(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="Keep this")
    store.append_message(
        conversation_id,
        role="assistant",
        content="Do not restore this partial answer",
        metadata={"interrupted": True},
    )
    provider = FakeProvider()

    asyncio.run(checkpoint_conversation_summary(store, conversation_id, provider))

    prompt = provider.calls[0][1]["content"]
    assert "Keep this" in prompt
    assert "partial answer" not in prompt


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
