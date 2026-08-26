"""End the active call when the machine runs out of RAM.

The KV cache mlx-lm retains between requests is capped by
``--prompt-cache-size``, but the cache of an *in-flight* generation is not
bounded by anything: a long enough call keeps growing its context until the
box swaps. Truncating that mid-conversation would silently lie to the user
about what the assistant remembers, so instead the call is ended.

Signals match scripts/native_memory_monitor.py so the guard and the
diagnostics agree about what "tight" means:

- ``memory_pressure`` free percentage (the macOS view of headroom)
- ``sysctl vm.swapusage`` used bytes (what actually hurts latency)

Both are macOS-only. Where they are unavailable (Linux containers) the guard
reports no pressure rather than guessing, so it never ends a call on a
platform it cannot measure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["MemoryReading", "MemoryGuardConfig", "read_memory", "evaluate", "guard_loop"]

GIB = 1024**3

_SWAP_RE = re.compile(
    r"used\s*=\s*(?P<used>[0-9.]+)(?P<unit>[KMG])",
    re.IGNORECASE,
)
_UNIT_BYTES = {"K": 1024, "M": 1024**2, "G": 1024**3}
_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(\d+)%")


@dataclass(frozen=True)
class MemoryReading:
    """A single sample. ``None`` means the signal was unavailable."""

    free_percent: int | None = None
    swap_used_bytes: int | None = None

    @property
    def measured(self) -> bool:
        return self.free_percent is not None or self.swap_used_bytes is not None


@dataclass(frozen=True)
class MemoryGuardConfig:
    enabled: bool = True
    min_free_percent: int = 10
    max_swap_bytes: int = 4 * GIB
    interval_seconds: float = 20.0
    # Number of consecutive tight readings before acting. Memory pressure spikes
    # briefly during model loads and tool bursts; ending a call on a single
    # sample would cut conversations short for a transient.
    consecutive_readings: int = 3

    @classmethod
    def from_env(cls) -> "MemoryGuardConfig":
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                return int(float(raw))
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
            min_free_percent=_int("CAAL_MEMORY_MIN_FREE_PERCENT", 10),
            max_swap_bytes=_int("CAAL_MEMORY_MAX_SWAP_GB", 4) * GIB,
            interval_seconds=float(_int("CAAL_MEMORY_CHECK_SECONDS", 20)),
            consecutive_readings=_int("CAAL_MEMORY_TIGHT_READINGS", 3),
        )


def _run(command: list[str], timeout: float = 10.0) -> str | None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_memory() -> MemoryReading:
    """Sample system memory headroom. Never raises."""
    free_percent: int | None = None
    swap_used: int | None = None

    pressure = _run(["memory_pressure"])
    if pressure:
        match = _FREE_RE.search(pressure)
        if match:
            free_percent = int(match.group(1))

    swap = _run(["sysctl", "vm.swapusage"])
    if swap:
        match = _SWAP_RE.search(swap)
        if match:
            unit = match.group("unit").upper()
            swap_used = int(float(match.group("used")) * _UNIT_BYTES[unit])

    return MemoryReading(free_percent=free_percent, swap_used_bytes=swap_used)


def evaluate(reading: MemoryReading, config: MemoryGuardConfig) -> str | None:
    """Return a human-readable reason if memory is too tight, else None."""
    if not reading.measured:
        return None

    if (
        reading.free_percent is not None
        and reading.free_percent < config.min_free_percent
    ):
        return (
            f"only {reading.free_percent}% of system memory free "
            f"(limit {config.min_free_percent}%)"
        )

    if (
        reading.swap_used_bytes is not None
        and reading.swap_used_bytes > config.max_swap_bytes
    ):
        return (
            f"swap at {reading.swap_used_bytes / GIB:.2f} GiB "
            f"(limit {config.max_swap_bytes / GIB:.2f} GiB)"
        )

    return None


async def guard_loop(on_pressure, config: MemoryGuardConfig | None = None) -> None:
    """Poll memory and invoke ``on_pressure(reason)`` when it stays tight.

    Runs until cancelled. ``on_pressure`` is awaited once and the loop then
    exits - ending the call is not something to do twice.
    """
    config = config or MemoryGuardConfig.from_env()
    if not config.enabled:
        logger.info("Memory guard disabled")
        return

    probe = await asyncio.to_thread(read_memory)
    if not probe.measured:
        logger.info("Memory guard inactive (no readable memory signals)")
        return

    logger.info(
        "Memory guard active (min_free=%d%%, max_swap=%.1f GiB, every %.0fs)",
        config.min_free_percent,
        config.max_swap_bytes / GIB,
        config.interval_seconds,
    )

    tight = 0
    while True:
        await asyncio.sleep(config.interval_seconds)
        reading = await asyncio.to_thread(read_memory)
        reason = evaluate(reading, config)
        if reason is None:
            tight = 0
            continue

        tight += 1
        logger.warning(
            "Memory pressure %d/%d: %s", tight, config.consecutive_readings, reason
        )
        if tight >= config.consecutive_readings:
            await on_pressure(reason)
            return
