import asyncio

from caal.integrations.memory_tool import MEMORY_SHORT_TOOL_DEF, execute_memory_short
from caal.memory.short_term import ShortTermMemory


def memory_with(entries):
    memory = object.__new__(ShortTermMemory)
    memory._cache = {"entries": entries}
    return memory


def test_injected_memory_explicitly_belongs_to_user():
    memory = memory_with(
        {
            "self_description": {
                "value": "Meticulous — you notice small details.",
                "stored_at": 1,
                "expires_at": None,
                "source": "ui",
            }
        }
    )

    context = memory.get_context_message()

    assert context is not None
    assert "Every entry below belongs to the user" in context
    assert "These entries never describe the assistant" in context
    assert "Interpret 'you' inside a value as referring to the user" in context
    assert '- User memory "self_description": Meticulous' in context


def test_recall_result_labels_value_as_user_memory():
    memory = memory_with(
        {
            "music_preference": {
                "value": "jazz and funky music",
                "stored_at": 1,
                "expires_at": None,
                "source": "explicit",
            }
        }
    )

    result = asyncio.run(
        execute_memory_short(memory, "recall", key="music_preference")
    )

    assert result == 'User memory "music_preference": jazz and funky music'


def test_tool_definition_forbids_assistant_attribution():
    description = MEMORY_SHORT_TOOL_DEF["function"]["description"]

    assert "Memory entries never describe the assistant" in description
    assert "Always attribute recalled entries to the user" in description
