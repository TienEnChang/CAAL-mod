"""
LLM handling with provider-agnostic architecture.

Supports multiple LLM backends (Ollama, Groq) with unified tool calling.
"""

from .caal_llm import CAALLLM
from .llm_node import ToolDataCache, discover_tools, llm_node, unbound_tool_names

# Backward compatibility aliases
from .ollama_llm import OllamaLLM
from .ollama_node import OllamaLLMNode, ollama_llm_node
from .providers import (
    GroqProvider,
    LLMProvider,
    OllamaProvider,
    create_provider,
    create_provider_from_settings,
)

__all__ = [
    # New API
    "CAALLLM",
    "llm_node",
    "ToolDataCache",
    "discover_tools",
    "unbound_tool_names",
    "LLMProvider",
    "OllamaProvider",
    "GroqProvider",
    "create_provider",
    "create_provider_from_settings",
    # Backward compatibility
    "OllamaLLM",
    "OllamaLLMNode",
    "ollama_llm_node",
]
