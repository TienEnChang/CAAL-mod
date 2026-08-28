"""End a chat session before the active model exhausts the Mac.

The guard has two signals with different meanings:

* MLX active allocation is the primary backend-local signal. At the configured
  limit the session ends normally, ordinary request teardown releases transient
  allocations, and the model process restarts only if recovery fails. Process
  footprint supplies a fallback when MLX metrics are unavailable.
* macOS critical memory pressure is a system-wide emergency. The model is
  reloaded before the session is replaced so the reconnect procedure has enough
  memory to complete.

The CAAL-owned server publishes MLX allocation counters at ``GET /v1/memory``.
Its physical footprint is read from macOS when those counters are unavailable.

macOS pressure is observed with ``DISPATCH_SOURCE_TYPE_MEMORYPRESSURE`` and
``kern.memorystatus_vm_pressure_level``. The dispatch source reports only
transitions, so it reads as unknown until the first one arrives; the sysctl is
pollable and supplies the level in the meantime.

Urgent pressure is diagnostic state only. Only critical pressure trips the
guard and requests restart-first recovery.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .model_cache import read_local_model_memory

logger = logging.getLogger(__name__)

GIB = 1024**3

# CAAL's model server is an ordinary python process, so it is identified by
# the script it runs rather than by its executable name.
MODEL_SERVER_SCRIPT = "mlx_model_server.py"

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


class PressureLevel(IntEnum):
    """Semantic XNU memory-pressure states."""

    NORMAL = 0
    WARNING = 1
    URGENT = 2
    CRITICAL = 3


@dataclass(frozen=True)
class MemoryTrip:
    reason: str
    restart_first: bool = False


@dataclass(frozen=True)
class MemoryReading:
    """One sample. ``None`` means that signal was unavailable."""

    model_footprint_bytes: int | None = None
    pressure: PressureLevel | None = None
    model_active_bytes: int | None = None

    @property
    def measured(self) -> bool:
        return (
            self.model_footprint_bytes is not None
            or self.model_active_bytes is not None
            or self.pressure is not None
        )


@dataclass(frozen=True)
class MemoryGuardConfig:
    enabled: bool = True
    # One ceiling, 8 GiB, whichever signal is readable. MLX's active allocation
    # is the primary reading; the macOS footprint is only consulted when the
    # model server cannot be reached. Holding both at the same number means the
    # call ends at the same point either way, rather than the guard becoming
    # stricter or looser depending on which signal happened to be available.
    max_model_bytes: int = 8 * GIB
    recovery_model_bytes: int = 6 * GIB
    max_active_bytes: int = 8 * GIB
    recovery_active_bytes: int = int(4.5 * GIB)
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
            max_model_bytes=int(_num("CAAL_MODEL_MEMORY_TRIP_GB", 8) * GIB),
            recovery_model_bytes=int(_num("CAAL_MODEL_MEMORY_RECOVERY_GB", 6) * GIB),
            max_active_bytes=int(_num("CAAL_MODEL_ALLOCATION_TRIP_GB", 8) * GIB),
            recovery_active_bytes=int(
                _num("CAAL_MODEL_ALLOCATION_RECOVERY_GB", 4.5) * GIB
            ),
            interval_seconds=_num("CAAL_MEMORY_CHECK_SECONDS", 2),
            consecutive_readings=int(_num("CAAL_MEMORY_TIGHT_READINGS", 2)),
            recovery_timeout_seconds=_num("CAAL_MEMORY_RECOVERY_TIMEOUT", 20),
        )


class _MacOSPressureSource:
    """Keep the latest pressure transition reported by libdispatch."""

    _MASK = 1 | 2 | 4  # Public libdispatch normal/warning/critical bit masks.

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
                if data & 4:
                    self._level = PressureLevel.CRITICAL
                elif data & 2:
                    self._level = PressureLevel.WARNING
                elif data & 1:
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


def _sysctl_pressure_level() -> PressureLevel | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            level = int(result.stdout.strip())
            if level == 0:
                return PressureLevel.NORMAL
            if level == 1:
                return PressureLevel.WARNING
            if level == 2:
                return PressureLevel.URGENT
            if level >= 3:
                return PressureLevel.CRITICAL
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _model_server_pids() -> list[int]:
    """Return the pids serving CAAL's model.

    Matching on the script path rather than a bare "python" keeps unrelated
    interpreters - the agent, the speech bridge, this process - out of the
    model lane, while still finding the server wherever its venv lives.
    """
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listing.returncode != 0:
        return []

    pids: list[int] = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # An interpreter running the script, not any process whose arguments
        # happen to name it: a shell tailing the log must not be counted as the
        # model. Requiring both the python executable and the script as its own
        # argument is what separates the two.
        if not os.path.basename(parts[1]).startswith("python"):
            continue
        if not any(os.path.basename(arg) == MODEL_SERVER_SCRIPT for arg in parts[2:]):
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return pids


def _model_footprint_bytes() -> int | None:
    """Sum the physical footprint of CAAL's model server."""
    if sys.platform != "darwin":
        return None
    pids = _model_server_pids()
    if not pids:
        return None

    descriptor, name = tempfile.mkstemp(prefix="caal-guard-footprint-", suffix=".json")
    os.close(descriptor)
    report = Path(name)
    command = ["/usr/bin/footprint"]
    for pid in pids:
        command.extend(["-p", str(pid)])
    command.extend(["-j", str(report), "-f", "bytes"])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode != 0:
            return None
        payload = json.loads(report.read_text())
        return sum(
            int(
                process.get("auxiliary", {}).get(
                    "phys_footprint", process.get("footprint", 0)
                )
            )
            for process in payload.get("processes", [])
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    finally:
        report.unlink(missing_ok=True)


def read_memory(model_base_url: str | None = None) -> MemoryReading:
    """Sample the model footprint and macOS pressure state. Never raises."""
    # NORMAL is zero, so an unreadable level must be distinguished explicitly
    # rather than by truthiness; treating it as missing would leave the guard
    # with no signal at all on a machine that is simply healthy.
    pressure = _sysctl_pressure_level()
    if pressure is None:
        pressure = _PRESSURE_SOURCE.current()
    active_bytes = None
    if model_base_url:
        payload = read_local_model_memory(model_base_url)
        if payload is not None:
            try:
                active_bytes = int(payload["active_bytes"])
            except (KeyError, TypeError, ValueError):
                pass
    return MemoryReading(
        model_footprint_bytes=(
            _model_footprint_bytes() if active_bytes is None else None
        ),
        pressure=pressure,
        model_active_bytes=active_bytes,
    )


def _read_for_endpoint(model_base_url: str | None) -> MemoryReading:
    return read_memory(model_base_url) if model_base_url else read_memory()


def evaluate(reading: MemoryReading, config: MemoryGuardConfig) -> MemoryTrip | None:
    """Return the required action when a guard signal trips."""
    if reading.pressure == PressureLevel.CRITICAL:
        return MemoryTrip(
            "macOS reports critical memory pressure",
            restart_first=True,
        )

    if (
        reading.model_active_bytes is not None
        and reading.model_active_bytes >= config.max_active_bytes
    ):
        return MemoryTrip(
            f"MLX is actively holding {reading.model_active_bytes / GIB:.2f} GiB "
            f"(limit {config.max_active_bytes / GIB:.2f} GiB)"
        )

    if (
        reading.model_active_bytes is None
        and reading.model_footprint_bytes is not None
        and reading.model_footprint_bytes >= config.max_model_bytes
    ):
        return MemoryTrip(
            f"The model is holding {reading.model_footprint_bytes / GIB:.2f} GiB "
            f"(limit {config.max_model_bytes / GIB:.2f} GiB)"
        )

    return None


async def wait_for_recovery(
    config: MemoryGuardConfig,
    model_base_url: str | None = None,
) -> bool:
    """Wait until request teardown demonstrably reduced the model footprint."""
    deadline = asyncio.get_running_loop().time() + config.recovery_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        reading = await asyncio.to_thread(_read_for_endpoint, model_base_url)
        if reading.pressure == PressureLevel.CRITICAL:
            return False
        if (
            reading.model_active_bytes is not None
            and reading.model_active_bytes < config.recovery_active_bytes
        ):
            return True
        if (
            reading.model_active_bytes is None
            and reading.model_footprint_bytes is not None
            and reading.model_footprint_bytes < config.recovery_model_bytes
        ):
            return True
        await asyncio.sleep(config.interval_seconds)
    return False


async def guard_loop(
    on_trip,
    config: MemoryGuardConfig | None = None,
    model_base_url: str | None = None,
) -> None:
    """Poll the two signals and invoke ``on_trip`` once."""
    config = config or MemoryGuardConfig.from_env()
    if not config.enabled:
        logger.info("Memory guard disabled")
        return

    probe = await asyncio.to_thread(_read_for_endpoint, model_base_url)
    if not probe.measured:
        logger.info("Memory guard inactive (no readable memory signals)")
        return

    logger.info(
        "Memory guard active (MLX<%.1f GiB, critical OS pressure, every %.1fs)",
        config.max_active_bytes / GIB,
        config.interval_seconds,
    )

    previous: MemoryTrip | None = None
    consecutive = 0
    while True:
        await asyncio.sleep(config.interval_seconds)
        reading = await asyncio.to_thread(_read_for_endpoint, model_base_url)
        trip = evaluate(reading, config)
        if trip is None:
            previous = None
            consecutive = 0
            continue

        # Critical pressure must reclaim the model before session teardown;
        # only footprint samples need the consecutive-reading filter below.
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
