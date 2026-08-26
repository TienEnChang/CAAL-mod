"""Short-term memory for CAAL voice agent.

Provides session-persistent memory storage with optional TTL-based expiry.
Memory survives agent restarts via file-based JSON persistence.

Three storage mechanisms feed into this:
    1. Auto-store: Tool responses with memory_hint field
    2. Explicit: memory_short tool with store/get/delete/list actions
    3. API: HTTP endpoints for external systems

Example:
    >>> from caal.memory import ShortTermMemory
    >>> memory = ShortTermMemory()
    >>> memory.store("flight_number", "UA1234", source="explicit")
    >>> memory.get("flight_number")
    'UA1234'
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .base import (
    DEFAULT_TTL_SECONDS,
    MEMORY_DIR,
    MemoryEntry,
    MemorySource,
    MemoryStore,
)

logger = logging.getLogger(__name__)

# File path for short-term memory persistence
SHORT_TERM_MEMORY_PATH = MEMORY_DIR / "short_term_memory.json"


class ShortTermMemory:
    """Global singleton for short-term memory.

    Thread-safe, file-backed storage with in-memory cache.
    Automatically cleans up expired entries on access.

    Attributes:
        _instance: Singleton instance (class-level)
        _cache: In-memory cache of the memory store
    """

    _instance: ShortTermMemory | None = None
    _cache: MemoryStore | None = None

    def __new__(cls) -> ShortTermMemory:
        """Singleton pattern - returns existing instance or creates new one."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def store(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
        source: MemorySource = "explicit",
    ) -> None:
        """Store a value in memory.

        Args:
            key: Descriptive key (e.g., "flight_number", "tracking_code")
            value: Any JSON-serializable data to store
            ttl_seconds: Time-to-live in seconds. None for no expiry.
            source: Origin of data ("tool_hint", "explicit", "api")
        """
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None

        entry: MemoryEntry = {
            "value": value,
            "stored_at": now,
            "expires_at": expires_at,
            "source": source,
        }

        self._ensure_cache()
        self._cache["entries"][key] = entry
        self._save()

        ttl_str = f"ttl={ttl_seconds}s" if ttl_seconds else "no expiry"
        logger.info(f"Stored memory: {key} (source={source}, {ttl_str})")

    def get(self, key: str) -> Any | None:
        """Get a value from memory.

        Automatically removes the entry if it has expired.

        Args:
            key: The memory key to retrieve

        Returns:
            The stored value, or None if not found or expired
        """
        self._ensure_cache()
        entry = self._cache["entries"].get(key)

        if entry is None:
            return None

        # Check expiry
        if entry["expires_at"] is not None and time.time() > entry["expires_at"]:
            self.delete(key)
            logger.debug(f"Memory expired: {key}")
            return None

        return entry["value"]

    def delete(self, key: str) -> bool:
        """Delete a key from memory.

        Args:
            key: The memory key to delete

        Returns:
            True if key existed and was deleted, False otherwise
        """
        self._ensure_cache()

        if key in self._cache["entries"]:
            del self._cache["entries"][key]
            self._save()
            logger.info(f"Deleted memory: {key}")
            return True

        return False

    def list_keys(self) -> list[dict[str, Any]]:
        """List all non-expired memory keys with metadata.

        Automatically cleans up expired entries.

        Returns:
            List of dicts with key, stored_at, expires_at, source
        """
        self._ensure_cache()
        self.cleanup_expired()

        result = []
        for key, entry in self._cache["entries"].items():
            result.append({
                "key": key,
                "stored_at": entry["stored_at"],
                "expires_at": entry["expires_at"],
                "source": entry["source"],
            })

        return result

    def get_all(self) -> list[dict[str, Any]]:
        """Get all non-expired memory entries with full data.

        Returns:
            List of dicts with key, value, stored_at, expires_at, source
        """
        self._ensure_cache()
        self.cleanup_expired()

        result = []
        for key, entry in self._cache["entries"].items():
            result.append({
                "key": key,
                "value": entry["value"],
                "stored_at": entry["stored_at"],
                "expires_at": entry["expires_at"],
                "source": entry["source"],
            })

        return result

    def get_context_message(self) -> str | None:
        """Format memory as context string for LLM injection.

        Returns:
            Formatted string for system message, or None if empty
        """
        self._ensure_cache()
        self.cleanup_expired()

        if not self._cache["entries"]:
            return None

        now = time.time()
        lines = [
            "[USER MEMORY - Every entry below belongs to the user and describes "
            "the user's facts, preferences, or task context. These entries never "
            "describe the assistant's identity, traits, preferences, or experiences. "
            "Interpret 'you' inside a value as referring to the user. Use silently "
            "when relevant and do not read aloud unless the user asks.]"
        ]

        for key, entry in self._cache["entries"].items():
            value = entry["value"]
            source = entry["source"]

            # Format value (truncate if too long)
            if isinstance(value, (dict, list)):
                value_str = json.dumps(value, ensure_ascii=False)
            else:
                value_str = str(value)

            if len(value_str) > 100:
                value_str = value_str[:97] + "..."

            # Format expiry
            if entry["expires_at"] is not None:
                remaining = entry["expires_at"] - now
                if remaining > 3600:
                    expiry_str = f"{remaining / 3600:.1f}h"
                elif remaining > 60:
                    expiry_str = f"{remaining / 60:.0f}m"
                else:
                    expiry_str = f"{remaining:.0f}s"
                expiry_info = f", expires: {expiry_str}"
            else:
                expiry_info = ", no expiry"

            lines.append(
                f'- User memory "{key}": {value_str} '
                f"(source: {source}{expiry_info})"
            )

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all memory entries."""
        self._cache = {"entries": {}}
        self._save()
        logger.info("Cleared all short-term memory")

    def cleanup_expired(self) -> int:
        """Remove all expired entries.

        Returns:
            Number of entries cleaned up
        """
        self._ensure_cache()
        now = time.time()
        expired_keys = []

        for key, entry in self._cache["entries"].items():
            if entry["expires_at"] is not None and now > entry["expires_at"]:
                expired_keys.append(key)

        for key in expired_keys:
            del self._cache["entries"][key]

        if expired_keys:
            self._save()
            logger.info(f"Cleaned up {len(expired_keys)} expired memory entries")

        return len(expired_keys)

    def _ensure_cache(self) -> None:
        """Ensure cache is loaded."""
        if self._cache is None:
            self._load()

    def _load(self) -> None:
        """Load memory from file."""
        if SHORT_TERM_MEMORY_PATH.exists():
            try:
                with open(SHORT_TERM_MEMORY_PATH) as f:
                    data = json.load(f)
                    self._cache = {"entries": data.get("entries", {})}
                    logger.debug(f"Loaded short-term memory from {SHORT_TERM_MEMORY_PATH}")
                    return
            except Exception as e:
                logger.warning(f"Failed to load short-term memory: {e}")

        self._cache = {"entries": {}}

    def _save(self) -> None:
        """Save memory to file."""
        if self._cache is None:
            return

        try:
            SHORT_TERM_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SHORT_TERM_MEMORY_PATH, "w") as f:
                json.dump(self._cache, f, indent=2)
            logger.debug(f"Saved short-term memory to {SHORT_TERM_MEMORY_PATH}")
        except Exception as e:
            logger.error(f"Failed to save short-term memory: {e}")

    def reload(self) -> None:
        """Force reload memory from disk."""
        self._cache = None
        self._load()
        logger.info("Reloaded short-term memory from disk")
