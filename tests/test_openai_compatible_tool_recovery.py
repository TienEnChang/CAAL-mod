from caal.llm.providers import OpenAICompatibleProvider

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_short",
            "parameters": {"type": "object"},
        },
    }
]


def test_recovers_registered_python_style_tool_call():
    call = OpenAICompatibleProvider.parse_text_tool_call(
        '(memory_short(action="recall", key="hobbies"))',
        TOOLS,
    )

    assert call is not None
    assert call.name == "memory_short"
    assert call.arguments == {"action": "recall", "key": "hobbies"}
    assert call.id.startswith("recovered_")


def test_ignores_tool_syntax_embedded_in_prose():
    call = OpenAICompatibleProvider.parse_text_tool_call(
        'I would call memory_short(action="recall", key="hobbies").',
        TOOLS,
    )

    assert call is None


def test_ignores_unregistered_or_executable_calls():
    assert (
        OpenAICompatibleProvider.parse_text_tool_call(
            '(unknown_tool(action="recall"))',
            TOOLS,
        )
        is None
    )
    assert (
        OpenAICompatibleProvider.parse_text_tool_call(
            '(memory_short(action=get_action()))',
            TOOLS,
        )
        is None
    )
