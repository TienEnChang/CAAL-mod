"""Clear completed-call caches from CAAL's local MLX Qwen server."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def clear_local_qwen_cache(base_url: str, timeout: float = 5.0) -> bool:
    """Clear Qwen's prompt cache when ``base_url`` points to this Mac."""
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]

    try:
        response = requests.post(f"{root}/v1/cache/clear", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as error:
        logger.warning("Could not clear local Qwen prompt cache: %s", error)
        return False

    logger.info("Cleared local Qwen prompt cache after call end")
    return True
