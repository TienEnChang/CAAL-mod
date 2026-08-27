"""Recover a local LM Studio model without assuming its architecture."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _local_root(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def reload_local_lmstudio_model(
    base_url: str,
    instance_id: str,
    *,
    model_key: str | None = None,
    lms_bin: str | None = None,
    context_length: int = 32768,
    parallel: int = 4,
    timeout: float = 120.0,
) -> bool:
    """Reload CAAL's named model instance and leave other instances untouched."""
    root = _local_root(base_url)
    if root is None:
        logger.warning("Refusing to reload a model on a non-local LM Studio endpoint")
        return False

    try:
        catalog = None
        catalog_deadline = time.monotonic() + min(timeout, 15)
        while time.monotonic() < catalog_deadline:
            try:
                candidate = requests.get(f"{root}/api/v1/models", timeout=5)
                candidate.raise_for_status()
                catalog = candidate
                break
            except requests.RequestException:
                time.sleep(1)
        if catalog is None:
            raise requests.RequestException("LM Studio model catalog is unavailable")

        models = catalog.json().get("models", [])
        resolved_model_key = model_key
        instance_loaded = False
        for entry in models:
            ids = {
                item.get("id")
                for item in entry.get("loaded_instances", [])
                if item.get("id")
            }
            if instance_id in ids:
                instance_loaded = True
                resolved_model_key = resolved_model_key or entry.get("key")
                break
        if not resolved_model_key:
            # Compatibility for callers that use the library key as the API ID.
            for entry in models:
                if entry.get("key") == instance_id:
                    resolved_model_key = instance_id
                    instance_loaded = bool(entry.get("loaded_instances"))
                    break
        if not resolved_model_key:
            raise ValueError(f"LM Studio model key for {instance_id!r} is unknown")

        if instance_loaded:
            for attempt in range(3):
                try:
                    response = requests.post(
                        f"{root}/api/v1/models/unload",
                        json={"instance_id": instance_id},
                        timeout=30,
                    )
                    response.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(1)

        executable = lms_bin or os.getenv("CAAL_LMS_BIN") or os.path.expanduser(
            "~/.lmstudio/bin/lms"
        )
        result = subprocess.run(
            [
                executable,
                "load",
                resolved_model_key,
                "--context-length",
                str(context_length),
                "--parallel",
                str(parallel),
                "--identifier",
                instance_id,
                "--yes",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    except (
        OSError,
        requests.RequestException,
        subprocess.SubprocessError,
        RuntimeError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        logger.error("Could not reload LM Studio model %s: %s", instance_id, error)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{root}/v1/models", timeout=3)
            response.raise_for_status()
            identifiers = {item["id"] for item in response.json().get("data", [])}
            if instance_id in identifiers:
                logger.info(
                    "LM Studio model %s reloaded as %s",
                    resolved_model_key,
                    instance_id,
                )
                return True
        except (requests.RequestException, TypeError, ValueError, KeyError):
            pass
        time.sleep(1)

    logger.error("LM Studio model %s did not become ready after reload", instance_id)
    return False
