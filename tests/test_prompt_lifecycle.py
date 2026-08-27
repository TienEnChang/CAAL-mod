import asyncio

from caal.llm.llm_node import discover_tools
from caal.llm.providers import LLMResponse
from caal.prompt_lifecycle import (
    build_session_context,
    compose_system_prompt,
    warm_prompt_prefix,
)


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return LLMResponse(content="OK", tool_calls=[])


def test_session_context_orders_date_memory_then_summary():
    context = build_session_context(
        "DATE CONTEXT",
        user_memory="USER MEMORY",
        conversation_recall="CONVERSATION SUMMARY",
    )

    assert context.index("DATE CONTEXT") < context.index("USER MEMORY")
    assert context.index("USER MEMORY") < context.index("CONVERSATION SUMMARY")


def test_stable_prompt_precedes_all_session_context():
    prompt = compose_system_prompt("SYSTEM AND TOOLS", "DATE AND HISTORY")

    assert prompt == "SYSTEM AND TOOLS\n\nDATE AND HISTORY"


def test_prefix_warmup_uses_exact_tools_and_bounded_generation(monkeypatch):
    monkeypatch.setenv("CAAL_PREFIX_WARM_TOKENS", "1")
    provider = FakeProvider()
    tools = [{"type": "function", "function": {"name": "weather"}}]

    warmed = asyncio.run(
        warm_prompt_prefix(
            provider,
            stable_prompt="STABLE",
            tools=tools,
            session_context="SESSION",
            history=[
                {
                    "id": "saved-id",
                    "role": "assistant",
                    "content": "RECENT",
                    "created_at": 10,
                }
            ],
        )
    )

    assert warmed is True
    call = provider.calls[0]
    assert call["tools"] is tools
    assert call["messages"][0]["content"] == "STABLE\n\nSESSION"
    assert call["messages"][1] == {"role": "assistant", "content": "RECENT"}
    assert call["kwargs"]["max_tokens"] == 1
    assert call["kwargs"]["temperature"] == 0


def test_tool_discovery_is_deduplicated_and_stably_ordered():
    duplicate = {"type": "function", "function": {"name": "z_tool"}}
    canonical = {
        "type": "function",
        "function": {"name": "z_tool", "description": "canonical"},
    }
    first = {"type": "function", "function": {"name": "a_tool"}}
    agent = type(
        "Agent",
        (),
        {
            "_llm_tools_cache": None,
            "_tools": [],
            "_caal_mcp_servers": {},
            "_n8n_workflow_tools": [duplicate, first],
            "_hass_tool_definitions": [],
            "_agent_tool_definitions": [canonical],
        },
    )()

    tools = asyncio.run(discover_tools(agent))

    assert [tool["function"]["name"] for tool in tools] == ["a_tool", "z_tool"]
    assert tools[1]["function"]["description"] == "canonical"
