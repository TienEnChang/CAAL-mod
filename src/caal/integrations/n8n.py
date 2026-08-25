"""n8n workflow discovery and tool wrapping.

Convention:
- All workflows use webhook triggers
- Webhook URL = http://HOST:PORT/webhook/{webhook_path}
- Webhook path is read from the webhook node's path parameter
- Workflow descriptions in webhook node notes document expected parameters
"""

import json
import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Cache for workflow details to avoid redundant MCP calls
_workflow_details_cache: dict[str, dict] = {}
_cache_timestamp: float = 0
_cache_ttl_seconds: float = 3600  # 1 hour TTL


async def discover_n8n_workflows(n8n_mcp, base_url: str) -> tuple[list[dict], dict[str, str]]:
    """Discover n8n workflows and create tool definitions.

    Reads the webhook path from each workflow's webhook node to build the
    correct URL. Falls back to the workflow name if no webhook node is found.

    Args:
        n8n_mcp: Initialized n8n MCP server client
        base_url: n8n base URL (e.g. http://192.168.1.100:5678)

    Returns:
        Tuple of (ollama_tools, webhook_path_map)
        - ollama_tools: List of tool dicts in Ollama format
        - webhook_path_map: Dict mapping tool_name -> webhook_path
    """
    tools = []
    workflow_name_map = {}

    # Check cache expiry
    current_time = time.time()
    global _cache_timestamp, _workflow_details_cache
    if current_time - _cache_timestamp > _cache_ttl_seconds:
        _workflow_details_cache.clear()
        _cache_timestamp = current_time
        logger.debug("Cleared workflow details cache (TTL expired)")

    try:
        # Get list of workflows (basic info only)
        result = await n8n_mcp._client.call_tool("search_workflows", {})
        workflows_data = parse_mcp_result(result)

        # n8n returns {"data": [...], "count": N}
        if isinstance(workflows_data, dict) and "data" in workflows_data:
            workflows = workflows_data["data"]
        else:
            logger.warning(f"Unexpected workflows format: {type(workflows_data)}")
            workflows = []

        logger.info(f"Loading {len(workflows)} n8n workflows:")

        for workflow in workflows:
            wf_name = workflow["name"]  # Original workflow name
            wf_id = workflow["id"]  # Need ID for get_workflow_details
            tool_name = sanitize_tool_name(wf_name)

            # Try to get detailed description and webhook path from workflow details
            description = ""
            schema = None
            webhook_path = None
            try:
                # Check cache first
                if wf_id not in _workflow_details_cache:
                    details_result = await n8n_mcp._client.call_tool(
                        "get_workflow_details",
                        {"workflowId": wf_id}
                    )
                    _workflow_details_cache[wf_id] = parse_mcp_result(details_result)

                workflow_details = _workflow_details_cache[wf_id]
                description, schema = extract_webhook_description(
                    workflow_details
                )
                webhook_path = _get_webhook_path(workflow_details)

            except Exception as e:
                logger.warning(f"Failed to get details for {wf_name}: {e}")

            # Fallback to root description or generic message
            if not description:
                description = (
                    workflow.get("description")
                    or f"Execute {tool_name} workflow"
                )

            # Use structured schema if available, otherwise open schema
            if schema:
                parameters = build_parameters_from_schema(schema)
            else:
                parameters = {
                    "type": "object",
                    "additionalProperties": True,
                }

            # Create Ollama tool definition
            tool = {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": description,
                    "parameters": parameters,
                },
            }
            tools.append(tool)
            # Use actual webhook path if available, fall back to workflow name
            workflow_name_map[tool_name] = webhook_path or wf_name
            logger.info(f"  ✓ {tool_name} -> /webhook/{webhook_path or wf_name}")

    except Exception as e:
        logger.warning(f"Failed to discover n8n workflows: {e}", exc_info=True)
    return tools, workflow_name_map


async def execute_n8n_workflow(base_url: str, webhook_path: str, arguments: dict) -> Any:
    """Execute an n8n workflow via POST request.

    Args:
        base_url: n8n base URL (e.g. http://192.168.1.100:5678)
        webhook_path: The webhook path from the workflow's webhook node
        arguments: Arguments to pass to the workflow as JSON body

    Returns:
        Workflow execution result (only final node output)
    """
    webhook_url = f"{base_url.rstrip('/')}/webhook/{webhook_path}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(webhook_url, json=arguments) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"Failed to execute n8n workflow {webhook_path}: {e}")
            raise


def extract_webhook_description(
    workflow_details: dict,
) -> tuple[str, dict | None]:
    """Extract description and optional schema from webhook node notes.

    Notes format (new — with ---schema):
        Track flights or view airport departures/arrivals

        ---schema
        {
          "action": {"type": "string", "enum": ["track", "departures"]},
          "flight_iata": {"type": "string", "description": "IATA code"}
        }

    Notes format (legacy — prose only):
        Full prose description with inline parameter docs...

    Args:
        workflow_details: Full workflow structure from get_workflow_details

    Returns:
        Tuple of (description, schema_dict_or_None).
        schema_dict keys are param names, values have type/enum/description.
    """
    notes = _get_webhook_notes(workflow_details)
    if not notes:
        return "", None

    # Split on ---schema fence
    if "---schema" in notes:
        parts = notes.split("---schema", 1)
        description = parts[0].strip()
        schema_text = parts[1].strip()
        try:
            schema = json.loads(schema_text)
            return description, schema
        except json.JSONDecodeError as e:
            logger.warning(
                f"Failed to parse ---schema JSON: {e}. "
                "Falling back to prose description."
            )
            # Fall through to return full notes as description

    return notes, None


def _get_webhook_notes(workflow_details: dict) -> str:
    """Find webhook trigger node and return its notes field."""
    for node in workflow_details.get("workflow", {}).get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook":
            notes = node.get("notes", "").strip()
            if notes:
                return notes
            node_desc = node.get("description", "").strip()
            if node_desc:
                return node_desc
    return ""


def _get_webhook_path(workflow_details: dict) -> str | None:
    """Find webhook trigger node and return its path parameter."""
    for node in workflow_details.get("workflow", {}).get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook":
            path = node.get("parameters", {}).get("path", "").strip()
            if path:
                return path
    return None


def build_parameters_from_schema(schema: dict) -> dict:
    """Convert ---schema JSON into OpenAI-format parameters object.

    Input (from webhook notes):
        {
          "action": {"type": "string", "enum": [...], "required": true},
          "flight_iata": {"type": "string", "description": "...", "for": ["track"]}
        }

    Output (OpenAI tool format):
        {
          "type": "object",
          "properties": {
            "action": {"type": "string", "enum": [...]},
            "flight_iata": {"type": "string", "description": "..."}
          },
          "required": ["action"]
        }

    Strips `for` (CAAL metadata) and `required` (moved to top-level array).
    """
    properties = {}
    required = []

    for param_name, param_def in schema.items():
        # Copy param definition, stripping CAAL-only fields
        clean_def = {
            k: v for k, v in param_def.items()
            if k not in ("required", "for")
        }
        properties[param_name] = clean_def

        # Collect required params
        if param_def.get("required") is True:
            required.append(param_name)

    result: dict = {
        "type": "object",
        "properties": properties,
    }
    if required:
        result["required"] = required
    return result


def sanitize_tool_name(name: str) -> str:
    """Convert workflow name to valid tool name (lowercase, underscores)."""
    return name.lower().replace(" ", "_").replace("-", "_")


# MCP endpoint suffixes, longest first so /mcp-server/http wins over /mcp-server.
_MCP_URL_SUFFIXES = ("/mcp-server/http", "/mcp-server/sse", "/mcp-server")


def mcp_url_to_base_url(mcp_url: str) -> str:
    """Derive the n8n base URL from its MCP endpoint URL.

    Webhooks are served from the instance root while MCP lives under
    /mcp-server, so that suffix is stripped:

        http://127.0.0.1:5678/mcp-server/http -> http://127.0.0.1:5678

    Only the known suffixes are removed, which keeps instances hosted
    under a path prefix intact:

        https://example.com/n8n/mcp-server/http -> https://example.com/n8n

    Any other URL is returned unchanged apart from a trailing slash.
    """
    trimmed = mcp_url.strip().rstrip("/")
    for suffix in _MCP_URL_SUFFIXES:
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def clear_caches() -> None:
    """Clear all n8n workflow caches for hot reload.

    Call this before re-discovering workflows to ensure fresh data.
    """
    global _workflow_details_cache, _cache_timestamp
    _workflow_details_cache.clear()
    _cache_timestamp = 0
    logger.info("Cleared n8n workflow caches")


def parse_mcp_result(result) -> Any:
    """Parse MCP tool result, extracting content."""
    # Handle different MCP response formats
    if hasattr(result, "content") and result.content:
        # Get the first content item
        content_item = result.content[0]

        # Extract text from content
        text = content_item.text if hasattr(content_item, "text") else str(content_item)

        # Try to parse as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # If not JSON, return as-is
            return text

    # Fallback: return result as-is
    return result
