"""
MCP integrations for voice assistant.
"""

from .hass import create_hass_tools, detect_hass_tool_prefix
from .mcp_loader import MCPServerConfig, initialize_mcp_servers, load_mcp_config
from .memory_tool import (
    MEMORY_SHORT_TOOL_DEF,
    MemoryTools,
    execute_memory_short,
)
from .n8n import (
    discover_n8n_workflows,
    execute_n8n_workflow,
    mcp_url_to_base_url,
)
from .web_search import WEB_SEARCH_TOOL_DEF, WebSearchTools, execute_web_search

__all__ = [
    "create_hass_tools",
    "detect_hass_tool_prefix",
    "discover_n8n_workflows",
    "execute_memory_short",
    "execute_n8n_workflow",
    "execute_web_search",
    "initialize_mcp_servers",
    "load_mcp_config",
    "mcp_url_to_base_url",
    "MCPServerConfig",
    "MEMORY_SHORT_TOOL_DEF",
    "MemoryTools",
    "WEB_SEARCH_TOOL_DEF",
    "WebSearchTools",
]
