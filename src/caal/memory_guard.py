"""End a chat session before local Qwen exhausts the Mac.

The guard has two signals with different meanings:

* MLX allocation is Qwen-local and leads system pressure. At the configured
  limit the session ends normally, its retained cache is cleared, and Qwen is
  restarted only if that normal teardown fails to reclaim memory.
* macOS critical memory pressure is a system-wide emergency. Qwen is restarted
  before the session is replaced so the reconnect procedure has enough memory
  to complete.

macOS pressure is observed with ``DISPATCH_SOURCE_TYPE_MEMORYPRESSURE``. It is
event driven and intentionally uses the public normal/warning/critical states
rather than deriving a second pressure policy from free RAM and swap.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryGuardConfig",
    "MemoryReading",
    "MemoryTrip",
    "PressureLevel",
    "evaluate",
    "guard_loop",
    "read_memory",
    "wait_for_recovery",
]

GIB = 1024**3


class PressureLevel(IntEnum):
    """Public libdispatch memory-pressure states."""

    NORMAL = 1
    WARNING = 2
    CRITICAL = 4


@dataclass(frozen=True)
class MemoryTrip:
    reason: str
    restart_first: bool = False


@dataclass(frozen=True)
class MemoryReading:
    """One sample. ``None`` means that signal was unavailable."""

    mlx_active_bytes: int | None = None
    pressure: PressureLevel | None = None

    @property
    def measured(self) -> bool:
        return self.mlx_active_bytes is not None or self.pressure is not None


@dataclass(frozen=True)
class MemoryGuardConfig:
    enabled: bool = True
    max_mlx_bytes: int = 6 * GIB
    recovery_mlx_bytes: int = int(4.5 * GIB)
    interval_seconds: float = 2.0
    consecutive_readings: int = 2
    recovery_timeout_seconds: float = 20.0

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
            recovery_mlx_bytes=int(_num("CAAL_QWEN_MEMORY_RECOVERY_GB", 4.5) * GIB),
            interval_seconds=_num("CAAL_MEMORY_CHECK_SECONDS", 2),
            consecutive_readings=int(_num("CAAL_MEMORY_TIGHT_READINGS", 2)),
            recovery_timeout_seconds=_num("CAAL_MEMORY_RECOVERY_TIMEOUT", 20),
        )


class _MacOSPressureSource:
    """Keep the latest pressure transition reported by libdispatch."""

    _MASK = (
        PressureLevel.NORMAL | PressureLevel.WARNING | PressureLevel.CRITICAL
    )

    def __init__(self) -> None:
        self._level: PressureLevel | None = None
        self._source: int | None = None
        self._handler = None
        self._pid: int | None = None

    def current(self) -> PressureLevel | None:
        # LiveKit may fork job processes after importing this module. A dispatch
        # source created in the parent does not survive that boundary, so each
        # process lazily creates its own source on first use.
        if sys.platform == "darwin" and self._pid != os.getpid():
            self._level = None
            self._source = None
            self._handler = None
            self._pid = os.getpid()
            self._start()
        return self._level

    def _start(self) -> None:
        try:
            dispatch = ctypes.CDLL("/usr/lib/system/libdispatch.dylib")
            source_type = ctypes.c_byte.in_dll(
                dispatch, "_dispatch_source_type_memorypressure"
            )

            dispatch.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
            dispatch.dispatch_get_global_queue.restype = ctypes.c_void_p
            dispatch.dispatch_source_create.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            dispatch.dispatch_source_create.restype = ctypes.c_void_p
            dispatch.dispatch_source_get_data.argtypes = [ctypes.c_void_p]
            dispatch.dispatch_source_get_data.restype = ctypes.c_ulong

            callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p)

            @callback_type
            def handle_pressure(_context) -> None:
                if self._source is None:
                    return
                data = dispatch.dispatch_source_get_data(self._source)
                if data & PressureLevel.CRITICAL:
                    self._level = PressureLevel.CRITICAL
                elif data & PressureLevel.WARNING:
                    self._level = PressureLevel.WARNING
                elif data & PressureLevel.NORMAL:
                    self._level = PressureLevel.NORMAL

            dispatch.dispatch_source_set_event_handler_f.argtypes = [
                ctypes.c_void_p,
                callback_type,
            ]
            dispatch.dispatch_activate.argtypes = [ctypes.c_void_p]

            queue = dispatch.dispatch_get_global_queue(0, 0)
            source = dispatch.dispatch_source_create(
                ctypes.c_void_p(ctypes.addressof(source_type)),
                0,
                int(self._MASK),
                queue,
            )
            if not source:
                return
            self._source = source
            self._handler = handle_pressure  # Keep the C callback alive.
            dispatch.dispatch_source_set_event_handler_f(source, handle_pressure)
            dispatch.dispatch_activate(source)
        except (AttributeError, OSError, ValueError):
            logger.info("macOS memory-pressure notifications are unavailable")


_PRESSURE_SOURCE = _MacOSPressureSource()


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
    """Sample Qwen allocation and the latest macOS pressure state. Never raises."""
    return MemoryReading(
        mlx_active_bytes=_mlx_active_bytes(qwen_base_url),
        pressure=_PRESSURE_SOURCE.current(),
    )


def evaluate(reading: MemoryReading, config: MemoryGuardConfig) -> MemoryTrip | None:
    """Return the required action when a guard signal trips."""
    if reading.pressure == PressureLevel.CRITICAL:
        return MemoryTrip(
            "macOS reports critical memory pressure",
            restart_first=True,
        )

    if (
        reading.mlx_active_bytes is not None
        and reading.mlx_active_bytes >= config.max_mlx_bytes
    ):
        return MemoryTrip(
            f"Qwen is holding {reading.mlx_active_bytes / GIB:.2f} GiB "
            f"(limit {config.max_mlx_bytes / GIB:.2f} GiB)"
        )

    return None


async def wait_for_recovery(
    config: MemoryGuardConfig, qwen_base_url: str | None = None
) -> bool:
    """Wait until cache teardown demonstrably reduced local Qwen allocation."""
    if not qwen_base_url:
        return True

    deadline = asyncio.get_running_loop().time() + config.recovery_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        reading = await asyncio.to_thread(read_memory, qwen_base_url)
        if reading.pressure == PressureLevel.CRITICAL:
            return False
        if (
            reading.mlx_active_bytes is not None
            and reading.mlx_active_bytes < config.recovery_mlx_bytes
        ):
            return True
        await asyncio.sleep(config.interval_seconds)
    return False


async def guard_loop(
    on_trip,
    config: MemoryGuardConfig | None = None,
    qwen_base_url: str | None = None,
) -> None:
    """Poll the two signals and invoke ``on_trip`` once."""
    config = config or MemoryGuardConfig.from_env()
    if not config.enabled:
        logger.info("Memory guard disabled")
        return

    probe = await asyncio.to_thread(read_memory, qwen_base_url)
    if not probe.measured:
        logger.info("Memory guard inactive (no readable memory signals)")
        return

    logger.info(
        "Memory guard active (MLX<%.1f GiB, critical OS pressure, every %.1fs)",
        config.max_mlx_bytes / GIB,
        config.interval_seconds,
    )

    previous: MemoryTrip | None = None
    consecutive = 0
    while True:
        await asyncio.sleep(config.interval_seconds)
        reading = await asyncio.to_thread(read_memory, qwen_base_url)
        trip = evaluate(reading, config)
        if trip is None:
            previous = None
            consecutive = 0
            continue

        # libdispatch already applies hysteresis to critical pressure. Act on it
        # immediately; only raw MLX samples need the consecutive-reading filter.
        if trip.restart_first:
            await on_trip(trip)
            return

        consecutive = (
            consecutive + 1
            if previous is not None
            and previous.restart_first == trip.restart_first
            else 1
        )
        previous = trip
        logger.warning(
            "Memory trip %d/%d: %s",
            consecutive,
            config.consecutive_readings,
            trip.reason,
        )
        if consecutive >= config.consecutive_readings:
            await on_trip(trip)
            return
