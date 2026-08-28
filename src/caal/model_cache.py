"""Talk to CAAL's local MLX model server about memory it is holding.

Every call here is a no-op against a remote endpoint: CAAL must keep working
when ``openai_base_url`` points at somebody else's OpenAI-compatible server,
and a cache on another machine is not ours to clear.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

__all__ = [
    "clear_local_model_cache",
    "local_model_root",
    "read_local_model_memory",
    "restart_local_model_server",
    "unload_local_model",
]


def local_model_root(base_url: str) -> str | None:
    """Return the server root when ``base_url`` is on this machine, else None."""
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def clear_local_model_cache(base_url: str, timeout: float = 5.0) -> bool:
    """Release the prompt cache while leaving the weights loaded.

    This is the graceful-transition half of the lifecycle: turn and session KV
    must not cross a call boundary, but the model/runtime foundation stays.
    """
    root = local_model_root(base_url)
    if root is None:
        return False
    try:
        response = requests.post(f"{root}/v1/cache/clear", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as error:
        logger.warning("Could not clear local model prompt cache: %s", error)
        return False
    logger.info("Cleared local model prompt cache after call end")
    return True


def unload_local_model(base_url: str, timeout: float = 30.0) -> bool:
    """Drop the weights so the next request reloads them - the hard reset."""
    root = local_model_root(base_url)
    if root is None:
        logger.warning("Refusing to unload a model on a non-local endpoint")
        return False
    try:
        response = requests.post(f"{root}/v1/model/unload", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as error:
        logger.error("Could not unload local model: %s", error)
        return False
    logger.warning("Unloaded local model to reclaim memory")
    return True


def restart_local_model_server(
    project_dir: str | Path,
    base_url: str,
    timeout: float = 20.0,
) -> bool:
    """Hard-reset the supervised model process and wait for its API."""
    root = local_model_root(base_url)
    if root is None:
        logger.warning("Refusing to restart a model on a non-local endpoint")
        return False
    script = Path(project_dir) / "start-native.sh"
    try:
        result = subprocess.run(
            [str(script), "--restart", "model"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("Could not restart local model server: %s", error)
        return False
    if result.returncode != 0:
        logger.error(
            "Model-server restart failed: %s",
            result.stderr.strip() or result.stdout.strip(),
        )
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{root}/v1/models", timeout=1)
            response.raise_for_status()
            logger.warning("Restarted local model server to reclaim memory")
            return True
        except requests.RequestException:
            time.sleep(0.25)
    logger.error("Model-server API did not recover within %.1fs", timeout)
    return False


def read_local_model_memory(base_url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """Read MLX's own allocation counters - the leading signal for pressure.

    Free-memory percentage and swap both lag; MLX's active allocation moves
    first. Returns None for a remote endpoint or an unreachable server.
    """
    root = local_model_root(base_url)
    if root is None:
        return None
    try:
        response = requests.get(f"{root}/v1/memory", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        logger.debug("Could not read local model memory: %s", error)
        return None
    return payload if isinstance(payload, dict) else None
