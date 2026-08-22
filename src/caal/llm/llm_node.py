"""Provider-agnostic LLM Node for CAAL.

This module provides a custom llm_node implementation that works with any
LLMProvider (Ollama, Groq, etc.) while maintaining full tool calling support.

Key Features:
- Provider-agnostic LLM calls via LLMProvider interface
- Tool discovery from @function_tool methods and MCP servers
- Tool execution routing (agent methods, n8n workflows, MCP tools)
- Streaming responses for best UX

Usage:
    class MyAgent(Agent):
        async def llm_node(self, chat_ctx, tools, model_settings):
            async for chunk in llm_node(
                self, chat_ctx, provider=self._provider
            ):
                yield chunk
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any

from ..integrations.n8n import execute_n8n_workflow
from ..memory import ShortTermMemory
from ..utils.formatting import strip_markdown_for_tts
from .providers import LLMProvider

if TYPE_CHECKING:
    from .providers import ToolCall

logger = logging.getLogger(__name__)

__all__ = ["llm_node", "ToolDataCache"]


class ToolDataCache:
    """Caches recent tool response data for context injection.

    Tool responses often contain structured data (IDs, arrays) that the LLM
    needs for follow-up calls. This cache preserves that data separately
    from chat history and injects it into context on each LLM call.
    """

    def __init__(self, max_entries: int = 3):
        self.max_entries = max_entries
        self._cache: list[dict] = []

    def add(self, tool_name: str, data: Any, arguments: dict | None = None) -> None:
        """Add tool call and response data to cache."""
        entry = {"tool": tool_name, "args": arguments, "data": data, "timestamp": time.time()}
        self._cache.append(entry)
        if len(self._cache) > self.max_entries:
            self._cache.pop(0)  # Remove oldest

    def get_context_message(self) -> str | None:
        """Format cached data as context string for LLM injection."""
        if not self._cache:
            return None
        parts = ["Recent tool calls and responses for reference:"]
        for entry in self._cache:
            args = json.dumps(entry['args']) if entry.get('args') else ''
            parts.append(f"\n{entry['tool']}({args}) → {json.dumps(entry['data'])}")
        return "\n".join(parts)

    def clear(self) -> None:
        """Clear the cache."""
        self._cache.clear()


async def llm_node(
    agent,
    chat_ctx,
    provider: LLMProvider,
    tool_data_cache: ToolDataCache | None = None,
    short_term_memory: ShortTermMemory | None = None,
    max_turns: int = 20,
) -> AsyncIterable[str]:
    """Provider-agnostic LLM node with tool calling support.

    This function should be called from an Agent's llm_node method override.

    Args:
        agent: The Agent instance (self)
        chat_ctx: Chat context from LiveKit
        provider: LLMProvider instance (OllamaProvider, GroqProvider, etc.)
        tool_data_cache: Cache for structured tool response data
        short_term_memory: Short-term memory for context persistence
        max_turns: Max conversation turns to keep in sliding window

    Yields:
        String chunks for TTS output

    Example:
        class MyAgent(Agent):
            async def llm_node(self, chat_ctx, tools, model_settings):
                async for chunk in llm_node(
                    self, chat_ctx, provider=self._provider
                ):
                    yield chunk
    """
    try:
        # Build messages from chat context with sliding window
        messages = _build_messages_from_context(
            chat_ctx,
            tool_data_cache=tool_data_cache,
            short_term_memory=short_term_memory,
            max_turns=max_turns,
        )

        # Discover tools from agent and MCP servers
        tools = await _discover_tools(agent, provider)
        if not tools:
            logger.warning("No tools discovered")

        # If tools available, loop non-streaming calls to support chaining
        # Model can call tool A → get result → call tool B → get result → text
        max_tool_rounds = 5
        tool_round = 0
        all_tool_names: list[str] = []
        all_tool_params: list[dict] = []

        if tools:
            while tool_round < max_tool_rounds:
                try:
                    response = await provider.chat(messages=messages, tools=tools)
                except Exception as tool_err:
                    # Tool call generation failed (e.g., model garbled tool name)
                    err_msg = str(tool_err)
                    # Wrong tool name — model called a non-existent tool
                    if "not found" in err_msg:
                        logger.warning(
                            f"LLM called non-existent tool: {err_msg}. "
                            "Falling back to streaming."
                        )
                        break

                    # Garbled tool call — leaked control tokens etc.
                    is_garbled = (
                        "tool_use_failed" in err_msg
                        or "Failed to call a function" in err_msg
                        or "[/THINK]" in err_msg
                        or "[TOOL_CALLS]" in err_msg
                        or "<function=" in err_msg
                    )
                    if is_garbled and tool_round == 0:
                        logger.warning(
                            f"Malformed tool call, retrying: {err_msg}"
                        )
                        try:
                            response = await provider.chat(
                                messages=messages, tools=tools
                            )
                        except Exception:
                            logger.warning(
                                "Retry failed, falling back to streaming"
                            )
                            break
                    elif is_garbled:
                        logger.warning(
                            f"Malformed tool call in round {tool_round + 1}, "
                            "streaming final response"
                        )
                        break
                    else:
                        raise  # Re-raise non-tool errors

                if not response.tool_calls:
                    # Model is done with tools
                    if response.content:
                        # Don't clear tool status — keep indicator showing
                        # which tools were used for this response
                        _emit_usage(agent, provider)
                        yield strip_markdown_for_tts(response.content)
                        return
                    break  # No content either, fall through to streaming


                # Execute tool calls
                tool_round += 1
                logger.info(
                    f"Tool round {tool_round}/{max_tool_rounds}: "
                    f"{len(response.tool_calls)} call(s)"
                )

                # Accumulate tool usage across rounds for frontend indicator
                all_tool_names.extend(tc.name for tc in response.tool_calls)
                all_tool_params.extend(tc.arguments for tc in response.tool_calls)

                if hasattr(agent, "_on_tool_status") and agent._on_tool_status:
                    await agent._on_tool_status(
                        True, all_tool_names, all_tool_params, status="running"
                    )

                try:
                    messages = await _execute_tool_calls(
                        agent,
                        messages,
                        response.tool_calls,
                        response.content,
                        provider=provider,
                        tool_data_cache=tool_data_cache,
                        short_term_memory=short_term_memory,
                    )
                except Exception:
                    if hasattr(agent, "_on_tool_status") and agent._on_tool_status:
                        await agent._on_tool_status(
                            True,
                            all_tool_names,
                            all_tool_params,
                            status="failed",
                        )
                    raise
                if hasattr(agent, "_on_tool_status") and agent._on_tool_status:
                    await agent._on_tool_status(
                        True,
                        all_tool_names,
                        all_tool_params,
                        status="complete",
                    )
                # Loop back — model sees tool results and decides: chain or respond

            if tool_round >= max_tool_rounds:
                logger.warning(
                    f"Hit max tool rounds ({max_tool_rounds}), streaming response"
                )

        # Stream final response (after tool chain or no tools)
        # Only clear tool indicator if no tools were called this turn
        if tool_round == 0 and hasattr(agent, "_on_tool_status") and agent._on_tool_status:
            import asyncio

            asyncio.create_task(agent._on_tool_status(False, [], []))

        if tool_round > 0:
            # After tool execution — pass tools so Ollama can validate
            # tool_calls in message history
            logger.info("Streaming response after tool execution...")
            try:
                async for chunk in provider.chat_stream(
                    messages=messages, tools=tools
                ):
                    yield strip_markdown_for_tts(chunk)
            except Exception as stream_err:
                # Safety fallback: strip tool messages and retry without tools
                logger.warning(
                    f"Post-tool streaming failed: {stream_err}. "
                    "Retrying with stripped tool messages..."
                )
                clean_messages = _strip_tool_messages(messages)
                async for chunk in provider.chat_stream(messages=clean_messages):
                    yield strip_markdown_for_tts(chunk)
        else:
            # No tools or no tool calls — plain streaming
            async for chunk in provider.chat_stream(messages=messages):
                yield strip_markdown_for_tts(chunk)

        # Report token usage from final LLM call
        _emit_usage(agent, provider)

    except Exception as e:
        logger.error(f"Error in llm_node: {e}", exc_info=True)
        yield f"I encountered an error: {e}"


def _emit_usage(agent, provider) -> None:
    """Report token usage from provider to agent callback if available."""
    last_usage = getattr(provider, "_last_usage", None)
    if last_usage and hasattr(agent, "_on_usage") and agent._on_usage:
        agent._on_usage(last_usage)


def _strip_tool_messages(messages: list[dict]) -> list[dict]:
    """Convert tool call/result messages to plain text.

    Ollama crashes if messages contain tool references but no tools are
    registered. This converts tool messages to plain text equivalents,
    preserving the context so the model knows what happened.
    """
    cleaned = []
    for msg in messages:
        if msg.get("role") == "tool":
            # Tool result → system message with the content
            cleaned.append({
                "role": "system",
                "content": f"Tool result: {msg.get('content', '')}",
            })
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Assistant tool call → plain assistant message
            parts = []
            if msg.get("content"):
                parts.append(msg["content"])
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args = func.get("arguments", {})
                parts.append(f"[Called {name} with {args}]")
            cleaned.append({
                "role": "assistant",
                "content": "\n".join(parts),
            })
        else:
            cleaned.append(msg)
    return cleaned


def _build_messages_from_context(
    chat_ctx,
    tool_data_cache: ToolDataCache | None = None,
    short_term_memory: ShortTermMemory | None = None,
    max_turns: int = 20,
) -> list[dict]:
    """Build messages with sliding window and context injection.

    Dynamic context (tool cache, memory) is appended to the system prompt
    rather than injected as separate system messages. This keeps a single
    [SYSTEM_PROMPT] block in the rendered prompt, matching the format the
    model was trained on.

    Message order:
    1. System prompt + appended context (always first, never trimmed)
    2. Chat history (sliding window applied)

    Args:
        chat_ctx: LiveKit chat context
        tool_data_cache: Cache of recent tool response data
        short_term_memory: Short-term memory for context awareness
        max_turns: Max conversation turns to keep (1 turn = user + assistant)
    """
    system_prompt = None
    chat_messages = []

    for item in chat_ctx.items:
        item_type = type(item).__name__

        if item_type == "ChatMessage":
            msg = {"role": item.role, "content": item.text_content}
            if item.role == "system":
                system_prompt = msg
            else:
                chat_messages.append(msg)
        elif item_type == "FunctionCall":
            try:
                # Arguments must be JSON string for Groq compatibility
                args = getattr(item, "arguments", {}) or {}
                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                chat_messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": item.id,
                                "type": "function",
                                "function": {
                                    "name": item.name,
                                    "arguments": args_str,
                                },
                            }
                        ],
                    }
                )
            except AttributeError:
                pass
        elif item_type == "FunctionCallOutput":
            try:
                chat_messages.append(
                    {
                        "role": "tool",
                        "content": str(item.content),
                        "tool_call_id": item.tool_call_id,
                    }
                )
            except AttributeError:
                pass

    # Build final message list
    messages = []

    # 1. System prompt with dynamic context appended (single [SYSTEM_PROMPT] block)
    if system_prompt:
        system_content = system_prompt["content"]

        # Append tool data context
        if tool_data_cache:
            context = tool_data_cache.get_context_message()
            if context:
                system_content += f"\n\n{context}"

        # Append short-term memory context (only after user has spoken)
        has_user_message = any(m["role"] == "user" for m in chat_messages)
        if short_term_memory and has_user_message:
            memory_context = short_term_memory.get_context_message()
            if memory_context:
                system_content += f"\n\n{memory_context}"

        messages.append({"role": "system", "content": system_content})

    # 4. Apply sliding window to chat history
    # max_turns * 2 accounts for user + assistant pairs
    max_messages = max_turns * 2
    if len(chat_messages) > max_messages:
        trimmed = len(chat_messages) - max_messages
        chat_messages = chat_messages[-max_messages:]
        logger.debug(f"Sliding window: trimmed {trimmed} old messages")

    messages.extend(chat_messages)
    return messages


async def _discover_tools(agent, provider: LLMProvider | None = None) -> list[dict] | None:
    """Discover tools from agent methods and MCP servers.

    Tools are cached on the agent instance after first discovery to avoid
    redundant MCP API calls on every user utterance.

    Args:
        agent: The agent instance (VoiceAssistant or ToolContext)
        provider: LLM provider — used to call prepare_tools() for
            model-specific tool transformations (e.g. stripping descriptions
            for FunctionGemma)
    """
    # Return cached tools if available
    if hasattr(agent, "_llm_tools_cache") and agent._llm_tools_cache is not None:
        return agent._llm_tools_cache

    tools = []

    # Get @function_tool decorated methods from agent (bound methods on class)
    if hasattr(agent, "_tools") and agent._tools:
        for tool in agent._tools:
            if hasattr(tool, "__func__"):
                func = tool.__func__
                name = func.__name__
                description = func.__doc__ or ""
                sig = inspect.signature(func)
                properties = {}
                required = []

                for param_name, param in sig.parameters.items():
                    if param_name == "self":
                        continue
                    param_type = "string"
                    if param.annotation is not inspect.Parameter.empty:
                        if param.annotation is str:
                            param_type = "string"
                        elif param.annotation is int:
                            param_type = "integer"
                        elif param.annotation is float:
                            param_type = "number"
                        elif param.annotation is bool:
                            param_type = "boolean"
                    properties[param_name] = {"type": param_type}
                    if param.default is inspect.Parameter.empty and param_name != "self":
                        required.append(param_name)

                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": description,
                            "parameters": {
                                "type": "object",
                                "properties": properties,
                                "required": required,
                            },
                        },
                    }
                )

    # Get MCP tools from all configured servers (except n8n and home_assistant)
    # n8n uses webhook-based workflow discovery, not direct MCP tools
    # home_assistant uses wrapper tool (hass) for simpler LLM interface
    if hasattr(agent, "_caal_mcp_servers") and agent._caal_mcp_servers:
        for server_name, server in agent._caal_mcp_servers.items():
            # Skip servers that use wrapper tools instead of raw MCP tools
            if server_name in ("n8n", "home_assistant"):
                continue

            mcp_tools = await _get_mcp_tools(server)
            # Prefix tools with server name to avoid collisions
            for tool in mcp_tools:
                original_name = tool["function"]["name"]
                tool["function"]["name"] = f"{server_name}__{original_name}"
            tools.extend(mcp_tools)
            if mcp_tools:
                logger.info(f"Added {len(mcp_tools)} tools from MCP server: {server_name}")

    # Add n8n workflow tools (webhook-based execution, separate from MCP)
    if hasattr(agent, "_n8n_workflow_tools") and agent._n8n_workflow_tools:
        tools.extend(agent._n8n_workflow_tools)

    # Add Home Assistant tools (only if HASS is connected)
    if hasattr(agent, "_hass_tool_definitions") and agent._hass_tool_definitions:
        tools.extend(agent._hass_tool_definitions)

    # Add agent-level tools (memory_short, web_search — non-LiveKit callers)
    if hasattr(agent, "_agent_tool_definitions") and agent._agent_tool_definitions:
        tools.extend(agent._agent_tool_definitions)

    # Let provider transform tools for its model (e.g. strip descriptions
    # for FunctionGemma).  Applied once before caching.
    if tools and provider is not None:
        tools = provider.prepare_tools(tools)

    # Cache tools on agent and return
    result = tools if tools else None
    agent._llm_tools_cache = result

    return result


async def _get_mcp_tools(mcp_server) -> list[dict]:
    """Get tools from an MCP server in OpenAI format."""
    tools = []

    if not mcp_server or not hasattr(mcp_server, "_client") or not mcp_server._client:
        return tools

    try:
        tools_result = await mcp_server._client.list_tools()
        if hasattr(tools_result, "tools"):
            for mcp_tool in tools_result.tools:
                # Convert MCP schema to OpenAI format
                parameters = {"type": "object", "properties": {}, "required": []}
                if hasattr(mcp_tool, "inputSchema") and mcp_tool.inputSchema:
                    schema = mcp_tool.inputSchema
                    if isinstance(schema, dict):
                        parameters = schema.copy()
                    elif hasattr(schema, "properties"):
                        parameters["properties"] = schema.properties or {}
                        parameters["required"] = getattr(schema, "required", []) or []

                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": mcp_tool.name,
                            "description": getattr(mcp_tool, "description", "") or "",
                            "parameters": parameters,
                        },
                    }
                )

        # Don't log here - caller logs the summary

    except Exception as e:
        logger.warning(f"Error getting MCP tools: {e}")

    return tools


async def _execute_tool_calls(
    agent,
    messages: list[dict],
    tool_calls: list["ToolCall"],
    response_content: str | None,
    provider: LLMProvider,
    tool_data_cache: ToolDataCache | None = None,
    short_term_memory: ShortTermMemory | None = None,
) -> list[dict]:
    """Execute tool calls and append results to messages.

    Args:
        agent: The agent instance
        messages: Current message list to append to
        tool_calls: List of normalized ToolCall objects
        response_content: Original LLM response content (if any)
        provider: LLM provider (for formatting tool results)
        tool_data_cache: Optional cache to store structured tool response data
        short_term_memory: Optional memory to store memory_hint from tool responses
    """
    # Add assistant message with tool calls
    tool_call_message = provider.format_tool_call_message(
        content=response_content,
        tool_calls=tool_calls,
    )
    messages.append(tool_call_message)

    # Deduplicate identical tool calls (same name + same args)
    seen = set()
    unique_tool_calls = []
    for tc in tool_calls:
        key = (tc.name, json.dumps(tc.arguments, sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique_tool_calls.append(tc)
        else:
            logger.info(f"Dedup: skipping duplicate {tc.name} call with identical args")
    tool_calls = unique_tool_calls

    # Execute each tool
    for tool_call in tool_calls:
        tool_name = tool_call.name
        arguments = tool_call.arguments
        logger.info(f"Executing tool: {tool_name} with args: {arguments}")

        try:
            tool_result = await _execute_single_tool(agent, tool_name, arguments)

            # Extract and store memory_hint from tool response (deterministic)
            # Supports two formats:
            #   {"memory_hint": {"key": "simple_value"}}  → 7d default TTL
            #   {"memory_hint": {"key": {"value": "...", "ttl": 3600}}}  → custom TTL
            #   {"memory_hint": {"key": {"value": "...", "ttl": null}}}  → no expiry
            if short_term_memory and isinstance(tool_result, dict):
                memory_hint = tool_result.get("memory_hint")
                if memory_hint and isinstance(memory_hint, dict):
                    from caal.memory.base import DEFAULT_TTL_SECONDS

                    for key, hint_value in memory_hint.items():
                        # Check if hint_value is extended format with ttl
                        if isinstance(hint_value, dict) and "value" in hint_value:
                            actual_value = hint_value["value"]
                            # "ttl" in dict distinguishes missing (use default) vs null (no expiry)
                            if "ttl" in hint_value:
                                ttl = hint_value["ttl"]  # Could be int or None
                            else:
                                ttl = DEFAULT_TTL_SECONDS
                        else:
                            actual_value = hint_value
                            ttl = DEFAULT_TTL_SECONDS
                        short_term_memory.store(
                            key=key,
                            value=actual_value,
                            ttl_seconds=ttl,
                            source="tool_hint",
                        )
                        logger.info(f"Stored memory hint from {tool_name}: {key}")

            # Cache structured data if present
            if tool_data_cache and isinstance(tool_result, dict):
                # Look for common data fields, otherwise cache the whole result
                data = (
                    tool_result.get("data")
                    or tool_result.get("results")
                    or tool_result
                )
                tool_data_cache.add(tool_name, data, arguments=arguments)
                logger.debug(f"Cached tool data for {tool_name}")

            # Format tool result - preserve JSON structure for LLM
            if isinstance(tool_result, dict):
                result_content = json.dumps(tool_result)
            else:
                result_content = str(tool_result)

            result_message = provider.format_tool_result(
                content=result_content,
                tool_call_id=tool_call.id,
                tool_name=tool_name,
            )
            messages.append(result_message)

        except Exception as e:
            error_msg = f"Error executing tool {tool_name}: {e}"
            logger.error(error_msg, exc_info=True)
            result_message = provider.format_tool_result(
                content=error_msg,
                tool_call_id=tool_call.id,
                tool_name=tool_name,
            )
            messages.append(result_message)

    return messages


async def _execute_single_tool(agent, tool_name: str, arguments: dict) -> Any:
    """Execute a single tool call.

    Routing priority:
    1. Home Assistant tools (callable dict)
    2. Agent methods (@function_tool decorated on class)
    3. n8n workflows (webhook-based execution)
    4. MCP servers (with server_name__tool_name prefix parsing)
    """
    # Check Home Assistant tools (callable functions stored in dict)
    if hasattr(agent, "_hass_tool_callables") and tool_name in agent._hass_tool_callables:
        logger.info(f"Calling HASS tool: {tool_name}")
        result = await agent._hass_tool_callables[tool_name](**arguments)
        logger.info(f"HASS tool {tool_name} completed")
        return result

    # Check if it's an agent method (decorated on class)
    if hasattr(agent, tool_name) and callable(getattr(agent, tool_name)):
        logger.info(f"Calling agent tool: {tool_name}")
        result = await getattr(agent, tool_name)(**arguments)
        logger.info(f"Agent tool {tool_name} completed")
        return result

    # Check if it's an n8n workflow
    if (
        hasattr(agent, "_n8n_workflow_name_map")
        and tool_name in agent._n8n_workflow_name_map
        and hasattr(agent, "_n8n_base_url")
        and agent._n8n_base_url
    ):
        logger.info(f"Calling n8n workflow: {tool_name}")
        workflow_name = agent._n8n_workflow_name_map[tool_name]
        result = await execute_n8n_workflow(
            agent._n8n_base_url, workflow_name, arguments
        )
        logger.info(f"n8n workflow {tool_name} completed")
        return result

    # Check MCP servers (with multi-server routing)
    if hasattr(agent, "_caal_mcp_servers") and agent._caal_mcp_servers:
        # Parse server name from prefixed tool name
        # Format: server_name__actual_tool (double underscore separator)
        if "__" in tool_name:
            server_name, actual_tool = tool_name.split("__", 1)
        else:
            # Unprefixed tools default to n8n server
            server_name, actual_tool = "n8n", tool_name

        if server_name in agent._caal_mcp_servers:
            server = agent._caal_mcp_servers[server_name]
            result = await _call_mcp_tool(server, actual_tool, arguments)
            if result is not None:
                return result

    raise ValueError(f"Tool {tool_name} not found")


async def _call_mcp_tool(mcp_server, tool_name: str, arguments: dict) -> Any | None:
    """Call a tool on an MCP server.

    Calls the tool directly without checking if it exists first - the MCP
    server will return an error if the tool doesn't exist.
    """
    if not mcp_server or not hasattr(mcp_server, "_client"):
        return None

    try:
        logger.info(f"Calling MCP tool: {tool_name}")
        result = await mcp_server._client.call_tool(tool_name, arguments)

        # Check for errors
        if result.isError:
            text_contents = []
            for content in result.content:
                if hasattr(content, "text") and content.text:
                    text_contents.append(content.text)
            error_msg = f"MCP tool {tool_name} error: {text_contents}"
            logger.error(error_msg)
            return error_msg

        # Extract text content
        text_contents = []
        for content in result.content:
            if hasattr(content, "text") and content.text:
                text_contents.append(content.text)

        return "\n".join(text_contents) if text_contents else "Tool executed successfully"

    except Exception as e:
        logger.warning(f"Error calling MCP tool {tool_name}: {e}")

    return None
