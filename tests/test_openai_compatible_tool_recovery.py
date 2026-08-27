import asyncio
from types import SimpleNamespace

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


def test_chat_honors_per_request_generation_bounds():
    provider = OpenAICompatibleProvider(
        model="caal-model",
        base_url="http://localhost:8100/v1",
        temperature=0.7,
        max_tokens=4096,
    )
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OK", tool_calls=None)
                )
            ]
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "warm"}],
            temperature=0,
            max_tokens=1,
        )
    )

    assert captured["temperature"] == 0
    assert captured["max_tokens"] == 1
