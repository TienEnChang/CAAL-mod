"""OpenAI-compatible LLM provider implementation.

Provides integration with any server exposing an OpenAI-compatible API,
including LiteLLM, vLLM, LocalAI, text-generation-inference, and others.
Uses the openai Python library for async API calls.
"""

from __future__ import annotations

import ast
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from .base import LLMProvider, LLMResponse, ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["OpenAICompatibleProvider"]

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible LLM provider.

    Works with any server exposing the OpenAI API specification:
        - LiteLLM: https://docs.litellm.ai/
        - vLLM: https://vllm.readthedocs.io/
        - LocalAI: https://localai.io/
        - text-generation-inference: https://huggingface.co/docs/text-generation-inference/
        - Any other OpenAI-compatible endpoint

    Features:
        - Async API calls via openai.AsyncOpenAI
        - Arguments returned as JSON string (parsed automatically)
        - Tool results require name field (OpenAI API spec)
        - Configurable base URL for custom servers
        - Optional API key for authenticated servers

    Args:
        model: Model name (server-specific, e.g., "gpt-3.5-turbo" or "mistral")
        base_url: Server URL (e.g., "http://localhost:8000/v1")
        api_key: Optional API key for authenticated servers
        temperature: Sampling temperature (0.0-2.0)
        max_tokens: Maximum tokens to generate
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens

        # Some servers reject empty string API key, use placeholder if not provided
        self._client = AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key or "not-needed",
        )

        logger.debug(f"OpenAICompatibleProvider initialized: {model} at {self._base_url}")

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model(self) -> str:
        return self._model

    @property
    def temperature(self) -> float:
        return self._temperature

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Execute non-streaming chat completion.

        Args:
            messages: List of message dicts
            tools: Optional tool definitions
            **kwargs: Additional options

        Returns:
            Normalized LLMResponse
        """
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self._temperature),
            "max_tokens": kwargs.get("max_tokens", self._max_tokens),
            "stream": False,
        }

        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**request_kwargs)

        # Extract from response format
        message = response.choices[0].message

        # Extract tool calls if present
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                # OpenAI API returns arguments as JSON string
                args = self.parse_tool_arguments(tc.function.arguments)
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                    )
                )

        content = message.content
        if not tool_calls and tools and content:
            recovered_call = self.parse_text_tool_call(content, tools)
            if recovered_call is not None:
                logger.warning(
                    "Recovered text-form tool call from OpenAI-compatible model: %s",
                    recovered_call.name,
                )
                tool_calls.append(recovered_call)
                content = None

        return LLMResponse(content=content, tool_calls=tool_calls)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Execute streaming chat completion.

        Args:
            messages: List of message dicts
            tools: Optional tool definitions (for validation of tool_calls in history)
            **kwargs: Additional options

        Yields:
            String chunks of response content
        """
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self._temperature),
            "max_tokens": kwargs.get("max_tokens", self._max_tokens),
            "stream": True,
        }

        # Include tools if provided (for validation of tool_calls in message history)
        # Set tool_choice="none" to prevent the model from making tool calls
        # in streaming mode - streaming is used for text responses only.
        # Without this, the model may generate tool call deltas instead of
        # content deltas, producing zero text output and a silent session.
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "none"

        stream = await self._client.chat.completions.create(**request_kwargs)

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def parse_tool_arguments(self, arguments: Any) -> dict[str, Any]:
        """Parse tool arguments from JSON string.

        OpenAI API returns tool call arguments as a JSON string.

        Args:
            arguments: JSON string or dict of arguments

        Returns:
            Parsed arguments dict
        """
        if isinstance(arguments, str):
            try:
                return json.loads(arguments)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse tool arguments: {arguments}")
                return {}
        if isinstance(arguments, dict):
            return arguments
        return {}

    @staticmethod
    def parse_text_tool_call(
        content: str,
        tools: list[dict[str, Any]],
    ) -> ToolCall | None:
        """Recover a Python-style call emitted as response text.

        Some small local models occasionally emit ``(tool_name(key="value"))``
        instead of populating the OpenAI ``tool_calls`` field. Only a complete
        response naming a registered tool with literal keyword arguments is
        accepted, so ordinary prose and executable expressions are ignored.
        """
        tool_names = {
            function.get("name")
            for tool in tools
            if isinstance(tool, dict)
            and isinstance((function := tool.get("function")), dict)
            and isinstance(function.get("name"), str)
        }

        try:
            expression = ast.parse(content.strip(), mode="eval").body
        except (SyntaxError, ValueError):
            return None

        if (
            not isinstance(expression, ast.Call)
            or not isinstance(expression.func, ast.Name)
            or expression.func.id not in tool_names
            or expression.args
            or any(keyword.arg is None for keyword in expression.keywords)
        ):
            return None

        arguments: dict[str, Any] = {}
        try:
            for keyword in expression.keywords:
                if keyword.arg in arguments:
                    return None
                arguments[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError):
            return None

        return ToolCall(
            id=f"recovered_{uuid.uuid4().hex}",
            name=expression.func.id,
            arguments=arguments,
        )

    def format_tool_result(
        self,
        content: str,
        tool_call_id: str | None,
        tool_name: str,
    ) -> dict[str, Any]:
        """Format tool result message for OpenAI-compatible API.

        OpenAI API requires the tool name in addition to tool_call_id.

        Args:
            content: Tool execution result as string
            tool_call_id: ID of the tool call being responded to
            tool_name: Name of the tool that was called

        Returns:
            Message dict for API
        """
        return {
            "role": "tool",
            "content": content,
            "tool_call_id": tool_call_id,
            "name": tool_name,  # Required by OpenAI API
        }
