"""Trip signals that end a chat session before Qwen exhausts the machine.

Three signals, one action. They are not independent protections - they are
three views of the same failure, ordered by how early they can be seen:

- ``M_MLX >= 6 GiB``  Qwen-local pressure. The only *leading* signal: MLX's
  own allocation is measurable before the system notices.
- ``A <= 20%``        System headroom is no longer healthy. Lags, because
  macOS swaps while still reporting moderate free memory - evicting pages
  raises the free percentage, so it reports the result of eviction rather
  than its cause.
- ``dS >= 1 GiB``     Pressure has already escaped into swap. Trailing by
  definition; it can confirm the failure but never prevent it.

Only the MLX cap can prevent Qwen-caused swap, and only if it is low enough
that Qwen cannot eat the reserved headroom during its worst transient
allocation - each request deep-copies the retained cache before extending it,
so a peak of roughly twice the cache must fit inside the budget.

Swap growth is measured against a per-session baseline rather than an absolute
ceiling because macOS retains swap long after the pressure that caused it.

The macOS tools are addressed absolutely: services run with a PATH that omits
/usr/sbin, so a bare "sysctl" raises FileNotFoundError and the swap signal
silently reads None.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryReading",
    "MemoryGuardConfig",
    "read_memory",
    "evaluate",
    "guard_loop",
    "wait_for_recovery",
]

GIB = 1024**3

MEMORY_PRESSURE_BIN = "/usr/bin/memory_pressure"
SYSCTL_BIN = "/usr/sbin/sysctl"

_SWAP_RE = re.compile(r"used\s*=\s*(?P<used>[0-9.]+)(?P<unit>[KMG])", re.IGNORECASE)
_UNIT_BYTES = {"K": 1024, "M": 1024**2, "G": 1024**3}
_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(\d+)%")


@dataclass(frozen=True)
class MemoryReading:
    """One sample. ``None`` means that signal was unavailable."""

    free_percent: int | None = None
    swap_used_bytes: int | None = None
    mlx_active_bytes: int | None = None

    @property
    def measured(self) -> bool:
        return any(
            v is not None
            for v in (self.free_percent, self.swap_used_bytes, self.mlx_active_bytes)
        )


@dataclass(frozen=True)
class MemoryGuardConfig:
    enabled: bool = True
    max_mlx_bytes: int = 6 * GIB
    min_free_percent: int = 20
    max_swap_growth_bytes: int = 1 * GIB
    # All three signals are cheap (~55ms combined), so they are polled far
    # faster than the damage accrues: swap has been measured climbing 1.4 GiB
    # in about ten seconds, which a 20s cadence cannot see in time.
    interval_seconds: float = 2.0
    # A single glitched reading should not end a call, but anything longer than
    # a couple of samples is slower than the failure it is meant to catch.
    consecutive_readings: int = 2
    # How long to keep refusing new sessions after a trip.
    recovery_free_percent: int = 30
    recovery_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "MemoryGuardConfig":
        def _num(name: str, default: float) -> float:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                logger.warning("Ignoring invalid %s=%r", name, raw)
                return default

        enabled = os.getenv("CAAL_MEMORY_GUARD", "true").strip().lower() not in {
            "false",
            "0",
            "no",
            "off",
        }
        return cls(
            enabled=enabled,
            max_mlx_bytes=int(_num("CAAL_QWEN_MEMORY_TRIP_GB", 6) * GIB),
            min_free_percent=int(_num("CAAL_MEMORY_MIN_FREE_PERCENT", 20)),
            max_swap_growth_bytes=int(_num("CAAL_MEMORY_MAX_SWAP_GROWTH_GB", 1) * GIB),
            interval_seconds=_num("CAAL_MEMORY_CHECK_SECONDS", 2),
            consecutive_readings=int(_num("CAAL_MEMORY_TIGHT_READINGS", 2)),
            recovery_free_percent=int(_num("CAAL_MEMORY_RECOVERY_FREE_PERCENT", 30)),
            recovery_timeout_seconds=_num("CAAL_MEMORY_RECOVERY_TIMEOUT", 120),
        )


def _run(command: list[str], timeout: float = 10.0) -> str | None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _mlx_active_bytes(qwen_base_url: str | None) -> int | None:
    """Read MLX's active allocation from a local Qwen server."""
    if not qwen_base_url:
        return None
    parsed = urlparse(qwen_base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    root = qwen_base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        with urllib.request.urlopen(f"{root}/v1/memory", timeout=5) as response:
            return int(json.load(response)["active_bytes"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def read_memory(qwen_base_url: str | None = None) -> MemoryReading:
    """Sample all three signals. Never raises."""
    free_percent: int | None = None
    swap_used: int | None = None

    if pressure := _run([MEMORY_PRESSURE_BIN]):
        if match := _FREE_RE.search(pressure):
            free_percent = int(match.group(1))

    if swap := _run([SYSCTL_BIN, "vm.swapusage"]):
        if match := _SWAP_RE.search(swap):
            swap_used = int(float(match.group("used")) * _UNIT_BYTES[match.group("unit").upper()])

    return MemoryReading(
        free_percent=free_percent,
        swap_used_bytes=swap_used,
        mlx_active_bytes=_mlx_active_bytes(qwen_base_url),
    )


def evaluate(
    reading: MemoryReading,
    config: MemoryGuardConfig,
    *,
    baseline_swap_bytes: int | None = None,
) -> str | None:
    """Return why the guard should trip, or None."""
    if not reading.measured:
        return None

    if (
        reading.mlx_active_bytes is not None
        and reading.mlx_active_bytes >= config.max_mlx_bytes
    ):
        return (
            f"Qwen is holding {reading.mlx_active_bytes / GIB:.2f} GiB "
            f"(limit {config.max_mlx_bytes / GIB:.2f} GiB)"
        )

    if reading.free_percent is not None and reading.free_percent <= config.min_free_percent:
        return (
            f"only {reading.free_percent}% of system memory available "
            f"(limit {config.min_free_percent}%)"
        )

    if reading.swap_used_bytes is not None and baseline_swap_bytes is not None:
        growth = reading.swap_used_bytes - baseline_swap_bytes
        if growth >= config.max_swap_growth_bytes:
            return (
                f"swap grew {growth / GIB:.2f} GiB during this session "
                f"(limit {config.max_swap_growth_bytes / GIB:.2f} GiB)"
            )

    return None


async def wait_for_recovery(
    config: MemoryGuardConfig, qwen_base_url: str | None = None
) -> bool:
    """Block until memory looks healthy again, or the timeout expires."""
    deadline = asyncio.get_running_loop().time() + config.recovery_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        reading = await asyncio.to_thread(read_memory, qwen_base_url)
        healthy_mlx = (
            reading.mlx_active_bytes is None
            or reading.mlx_active_bytes < config.max_mlx_bytes * 0.75
        )
        healthy_free = (
            reading.free_percent is None
            or reading.free_percent >= config.recovery_free_percent
        )
        if healthy_mlx and healthy_free:
            return True
        await asyncio.sleep(config.interval_seconds)
    return False


async def guard_loop(
    on_trip,
    config: MemoryGuardConfig | None = None,
    qwen_base_url: str | None = None,
) -> None:
    """Poll the three signals and invoke ``on_trip(reason)`` once."""
    config = config or MemoryGuardConfig.from_env()
    if not config.enabled:
        logger.info("Memory guard disabled")
        return

    probe = await asyncio.to_thread(read_memory, qwen_base_url)
    if not probe.measured:
        logger.info("Memory guard inactive (no readable memory signals)")
        return

    logger.info(
        "Memory guard active (MLX<%.1f GiB, free>%d%%, swap growth<%.1f GiB, every %.1fs)",
        config.max_mlx_bytes / GIB,
        config.min_free_percent,
        config.max_swap_growth_bytes / GIB,
        config.interval_seconds,
    )

    baseline_swap = probe.swap_used_bytes
    tight = 0
    while True:
        await asyncio.sleep(config.interval_seconds)
        reading = await asyncio.to_thread(read_memory, qwen_base_url)
        reason = evaluate(reading, config, baseline_swap_bytes=baseline_swap)
        if reason is None:
            tight = 0
            # Swap released: track the new floor so the next session is not
            # measured against a level the machine has already recovered from.
            if (
                reading.swap_used_bytes is not None
                and baseline_swap is not None
                and reading.swap_used_bytes < baseline_swap
            ):
                baseline_swap = reading.swap_used_bytes
            continue

        tight += 1
        logger.warning("Memory trip %d/%d: %s", tight, config.consecutive_readings, reason)
        if tight >= config.consecutive_readings:
            await on_trip(reason)
            return
