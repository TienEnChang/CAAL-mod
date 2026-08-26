"""Restart the local Qwen service when memory recovery fails."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _local_memory_url(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/v1/memory"


def restart_local_qwen(
    project_dir: Path,
    base_url: str,
    *,
    restart_timeout: float = 120.0,
    ready_timeout: float = 60.0,
) -> bool:
    """Restart local Qwen and wait until its memory endpoint is ready."""
    memory_url = _local_memory_url(base_url)
    if memory_url is None:
        logger.warning("Refusing to restart Qwen for a non-local endpoint")
        return False

    script = project_dir / "start-native.sh"
    try:
        result = subprocess.run(
            [str(script), "--restart", "qwen"],
            capture_output=True,
            text=True,
            timeout=restart_timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("Could not restart Qwen: %s", error)
        return False

    if result.returncode != 0:
        logger.error("Qwen restart failed: %s", result.stderr.strip())
        return False

    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(memory_url, timeout=2) as response:
                int(json.load(response)["active_bytes"])
            logger.info("Qwen restarted and is ready")
            return True
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            time.sleep(1)

    logger.error("Qwen did not become ready after restart")
    return False
