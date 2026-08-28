import asyncio

from caal.llm.llm_node import discover_tools, unbound_tool_names
from caal.llm.providers import LLMResponse
from caal.prompt_lifecycle import (
    build_session_context,
    build_stable_prompt_bundle,
    compose_system_prompt,
    load_stable_prompt_bundle,
    require_prompt_prefix_ready,
    save_stable_prompt_bundle,
    toolset_fingerprint,
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


def test_required_prefix_warmup_fails_the_preparation_stage(monkeypatch):
    class FailingProvider:
        async def chat(self, messages, tools=None, **kwargs):
            raise RuntimeError("model unavailable")

    monkeypatch.setenv("CAAL_PREFIX_WARM_TOKENS", "1")

    try:
        asyncio.run(
            require_prompt_prefix_ready(
                FailingProvider(),
                stable_prompt="STABLE",
                tools=[],
                label="Startup stable prefix",
            )
        )
    except RuntimeError as error:
        assert str(error) == "Startup stable prefix warm-up did not complete"
    else:
        raise AssertionError("Required prefix failure must stop preparation")


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


def test_stable_bundle_round_trip_is_canonical_and_validated(tmp_path):
    bundle = build_stable_prompt_bundle(
        stable_prompt="SYSTEM",
        tools=[
            {"type": "function", "function": {"name": "z"}},
            {"function": {"name": "a"}, "type": "function"},
        ],
        language="en",
        model="model-id",
        metadata={"n8n_workflow_name_map": {"a": "path"}},
    )
    path = tmp_path / "stable.json"
    save_stable_prompt_bundle(bundle, path)

    loaded = load_stable_prompt_bundle(
        language="en",
        model="model-id",
        stable_prompt="SYSTEM",
        path=path,
    )

    assert [tool["function"]["name"] for tool in loaded.tools] == ["a", "z"]
    assert loaded.toolset_fingerprint == toolset_fingerprint(loaded.tools)
    assert loaded.metadata["n8n_workflow_name_map"] == {"a": "path"}


def test_stable_bundle_rejects_a_different_model(tmp_path):
    bundle = build_stable_prompt_bundle(
        stable_prompt="SYSTEM",
        tools=[],
        language="en",
        model="old-model",
    )
    path = tmp_path / "stable.json"
    save_stable_prompt_bundle(bundle, path)

    try:
        load_stable_prompt_bundle(
            language="en",
            model="new-model",
            stable_prompt="SYSTEM",
            path=path,
        )
    except RuntimeError as error:
        assert "language/model" in str(error)
    else:
        raise AssertionError("Expected mismatched stable bundle to be rejected")


def test_stable_tool_bindings_are_checked_without_rediscovery():
    async def memory_short():
        return None

    agent = type(
        "BoundAgent",
        (),
        {
            "memory_short": staticmethod(memory_short),
            "_hass_tool_callables": {},
            "_n8n_workflow_name_map": {"calendar": "calendar-path"},
            "_caal_mcp_servers": {"weather": object()},
        },
    )()
    tools = [
        {"type": "function", "function": {"name": "memory_short"}},
        {"type": "function", "function": {"name": "calendar"}},
        {"type": "function", "function": {"name": "weather__forecast"}},
        {"type": "function", "function": {"name": "missing"}},
    ]

    assert unbound_tool_names(agent, tools) == ["missing"]
