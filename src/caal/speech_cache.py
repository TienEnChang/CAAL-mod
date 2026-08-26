"""Release completed-call buffers from CAAL's local MLX speech server."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def clear_local_speech_cache(base_url: str, timeout: float = 10.0) -> bool:
    """Clear MLX buffers when ``base_url`` points to this Mac."""
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
        logger.warning("Could not clear local speech MLX cache: %s", error)
        return False

    logger.info("Cleared local speech MLX cache after call end")
    return True
