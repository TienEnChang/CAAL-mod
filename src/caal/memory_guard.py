"""End a chat session before the active LM Studio model exhausts the Mac.

The guard has two signals with different meanings:

* The LM Studio runtime's physical footprint is backend-local and leads system
  pressure. At the configured limit the session ends normally, ordinary request
  teardown releases the transient allocations, and the model is reloaded only
  if that teardown fails to bring the footprint back down.
* macOS critical memory pressure is a system-wide emergency. The model is
  reloaded before the session is replaced so the reconnect procedure has enough
  memory to complete.

LM Studio publishes no allocation counter of its own: its REST API reports a
loaded instance's context configuration and its catalog reports the model's
static file size, neither of which tracks live memory. The footprint of the
llmster inference tree is therefore read from macOS itself, which keeps the
first signal accurate for any model or quantization LM Studio can serve. The
daemon tree is shared, so other simultaneously loaded LM Studio models also
contribute to this measurement.

macOS pressure is observed with ``DISPATCH_SOURCE_TYPE_MEMORYPRESSURE`` and
``kern.memorystatus_vm_pressure_level``. The dispatch source reports only
transitions, so it reads as unknown until the first one arrives; the sysctl is
pollable and supplies the level in the meantime.
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

logger = logging.getLogger(__name__)

GIB = 1024**3

# The LM Studio daemon. Its inference workers are ordinary node executables, so
# they are found by walking the process tree rather than by name.
LMSTUDIO_DAEMON_COMM = "llmster"

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

    @property
    def measured(self) -> bool:
        return self.model_footprint_bytes is not None or self.pressure is not None


@dataclass(frozen=True)
class MemoryGuardConfig:
    enabled: bool = True
    max_model_bytes: int = 8 * GIB
    recovery_model_bytes: int = 6 * GIB
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


def _process_tree(root_comm: str) -> list[int]:
    """Return the pids of every ``root_comm`` process and its descendants."""
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listing.returncode != 0:
        return []

    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for line in listing.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
        # Match the executable, never the whole command line: an unrelated
        # process whose arguments merely mention LM Studio is not LM Studio.
        if os.path.basename(parts[2]) == root_comm:
            roots.append(pid)

    tree: list[int] = []
    seen: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        tree.append(pid)
        pending.extend(children.get(pid, []))
    return tree


def _model_footprint_bytes() -> int | None:
    """Sum the physical footprint of the LM Studio inference tree."""
    if sys.platform != "darwin":
        return None
    pids = _process_tree(LMSTUDIO_DAEMON_COMM)
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


def read_memory() -> MemoryReading:
    """Sample the model footprint and macOS pressure state. Never raises."""
    # NORMAL is zero, so an unreadable level must be distinguished explicitly
    # rather than by truthiness; treating it as missing would leave the guard
    # with no signal at all on a machine that is simply healthy.
    pressure = _sysctl_pressure_level()
    if pressure is None:
        pressure = _PRESSURE_SOURCE.current()
    return MemoryReading(
        model_footprint_bytes=_model_footprint_bytes(),
        pressure=pressure,
    )


def evaluate(reading: MemoryReading, config: MemoryGuardConfig) -> MemoryTrip | None:
    """Return the required action when a guard signal trips."""
    if reading.pressure == PressureLevel.CRITICAL:
        return MemoryTrip(
            "macOS reports critical memory pressure",
            restart_first=True,
        )

    if (
        reading.model_footprint_bytes is not None
        and reading.model_footprint_bytes >= config.max_model_bytes
    ):
        return MemoryTrip(
            f"LM Studio is holding {reading.model_footprint_bytes / GIB:.2f} GiB "
            f"(limit {config.max_model_bytes / GIB:.2f} GiB)"
        )

    return None


async def wait_for_recovery(config: MemoryGuardConfig) -> bool:
    """Wait until request teardown demonstrably reduced the model footprint."""
    deadline = asyncio.get_running_loop().time() + config.recovery_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        reading = await asyncio.to_thread(read_memory)
        if reading.pressure == PressureLevel.CRITICAL:
            return False
        if (
            reading.model_footprint_bytes is not None
            and reading.model_footprint_bytes < config.recovery_model_bytes
        ):
            return True
        await asyncio.sleep(config.interval_seconds)
    return False


async def guard_loop(
    on_trip,
    config: MemoryGuardConfig | None = None,
) -> None:
    """Poll the two signals and invoke ``on_trip`` once."""
    config = config or MemoryGuardConfig.from_env()
    if not config.enabled:
        logger.info("Memory guard disabled")
        return

    probe = await asyncio.to_thread(read_memory)
    if not probe.measured:
        logger.info("Memory guard inactive (no readable memory signals)")
        return

    logger.info(
        "Memory guard active (model<%.1f GiB, critical OS pressure, every %.1fs)",
        config.max_model_bytes / GIB,
        config.interval_seconds,
    )

    previous: MemoryTrip | None = None
    consecutive = 0
    while True:
        await asyncio.sleep(config.interval_seconds)
        reading = await asyncio.to_thread(read_memory)
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
