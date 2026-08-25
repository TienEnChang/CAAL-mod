"""Short-term memory tool for CAAL.

Provides explicit memory operations for the LLM to store and retrieve data.
This is mechanism #2 of the three storage mechanisms:
    1. Auto-store: Tool responses with memory_hint field (automatic)
    2. Explicit: memory_short tool (this file) - user says "remember X"
    3. Passive: Tool descriptions instruct LLM to check memory first

Core logic is in execute_memory_short() — called by both the LiveKit
mixin (MemoryTools) and the non-LiveKit caller (ToolContext).
"""

import json
import logging
from typing import TYPE_CHECKING

from livekit.agents import function_tool

from ..memory.base import DEFAULT_TTL_SECONDS

if TYPE_CHECKING:
    from ..memory import ShortTermMemory

logger = logging.getLogger(__name__)

# OpenAI-format tool definition (shared by ToolContext)
MEMORY_SHORT_TOOL_DEF: dict = {
    "type": "function",
    "function": {
        "name": "memory_short",
        "description": (
            "The user's short-term memory — store and recall "
            "facts, preferences, and task context that belong to the user. "
            "Memory entries never describe the assistant.\n"
            "\n"
            "Actions:\n"
            "  store — save a value.\n"
            "  recall — retrieve a stored value.\n"
            "  list — show all stored memories.\n"
            "  delete — remove a stored value.\n"
            "\n"
            "Rules:\n"
            "- Always attribute recalled entries to the user, never to yourself.\n"
            "- Interpret second-person wording inside a value as describing the user.\n"
            "- Use descriptive key names "
            "(e.g. flight, email, dentist).\n"
            "- Default TTL is 7 days.\n"
            "- If memory data is already in "
            "context, use it directly without "
            "calling recall."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "One of: store, recall, "
                        "list, delete"
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Memory key name, e.g. "
                        "flight, email, package. "
                        "For: store, recall, delete"
                    ),
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Value to store. "
                        "For: store"
                    ),
                },
                "ttl": {
                    "type": "string",
                    "description": (
                        "Expiry: 1h, 6h, 1d, 7d, "
                        "30d, forever. Default 7d. "
                        "For: store"
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


def parse_ttl(ttl_str: str) -> int | None:
    """Parse a TTL string like '1h', '7d', 'never' into seconds."""
    ttl_str = ttl_str.strip().lower()
    if ttl_str in ("never", "none", "forever"):
        return None

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for suffix, mult in multipliers.items():
        if ttl_str.endswith(suffix):
            try:
                return int(ttl_str[:-1]) * mult
            except ValueError:
                break

    # Fall through to default
    return DEFAULT_TTL_SECONDS


async def execute_memory_short(
    memory: "ShortTermMemory | None",
    action: str,
    key: str = "",
    value: str = "",
    ttl: str = "",
) -> str:
    """Execute a memory_short operation.

    Core logic shared by LiveKit mixin and non-LiveKit ToolContext.
    """
    if memory is None:
        logger.warning("memory_short called but no memory instance available")
        return "Memory not available"

    logger.info(f"memory_short: action={action}, key={key}")

    if action == "store":
        if not key:
            return "Key is required for store action"
        if not value:
            return "Value is required for store action"

        # Parse value as JSON if it looks like JSON
        try:
            if value.startswith(("{", "[")):
                parsed_value = json.loads(value)
            else:
                parsed_value = value
        except json.JSONDecodeError:
            parsed_value = value

        # Parse TTL string to seconds
        ttl_seconds = parse_ttl(ttl) if ttl else None

        memory.store(
            key=key,
            value=parsed_value,
            ttl_seconds=ttl_seconds,
            source="explicit",
        )
        return f"Stored user memory: {key}"

    elif action in ("get", "recall"):
        if not key:
            return "Key is required for recall action"

        result = memory.get(key)
        if result is None:
            return f"No user memory found for key: {key}"

        if isinstance(result, (dict, list)):
            result = json.dumps(result)
        return f'User memory "{key}": {result}'

    elif action == "delete":
        if not key:
            return "Key is required for delete action"

        deleted = memory.delete(key)
        return (
            f"Deleted user memory: {key}"
            if deleted
            else f"User memory key not found: {key}"
        )

    elif action == "list":
        entries = memory.list_keys()
        if not entries:
            return "User memory is empty"

        lines = ["Stored user memory keys (all entries describe the user):"]
        for entry in entries:
            lines.append(f"- {entry['key']} (source: {entry['source']})")
        return "\n".join(lines)

    else:
        return (
            f"Unknown action: {action}. "
            "Valid actions: store, recall, delete, list"
        )


class MemoryTools:
    """Mixin providing memory_short tool for explicit user-memory operations.

    Requires the parent class to have:
    - self._short_term_memory: ShortTermMemory instance
    """

    @function_tool
    async def memory_short(
        self,
        action: str,
        key: str = "",
        value: str = "",
        ttl: str = "",
    ) -> str:
        """Store or retrieve information that belongs to the user.

        Use this to remember things the user tells you like flight numbers,
        tracking codes, preferences, or any data you'll need to reference later.
        Every stored entry describes the user's facts, preferences, or task
        context, never your own identity, traits, preferences, or experiences.
        Always attribute recalled information to the user. Interpret "you" in a
        stored value as referring to the user, not yourself.

        IMPORTANT: Before asking the user for information you've already
        discussed (like a tracking number or flight), check memory first
        with action="recall" or action="list".

        Args:
            action: One of "store", "recall", "delete", "list"
            key: The key to store/recall/delete (e.g., "flight_number", "tracking_code")
            value: The value to store (only required for action="store")
            ttl: Optional expiry for store action. Examples: "1h", "6h", "1d",
                "7d", "30d", "forever". Default is 7 days if not specified.

        Returns:
            Result of the operation
        """
        return await execute_memory_short(
            memory=getattr(self, "_short_term_memory", None),
            action=action,
            key=key,
            value=value,
            ttl=ttl,
        )
