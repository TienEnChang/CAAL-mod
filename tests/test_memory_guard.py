"""Tests for the model-footprint and macOS-pressure memory guard."""

import asyncio
from types import SimpleNamespace

import pytest

from caal.memory_guard import (
    GIB,
    MemoryGuardConfig,
    MemoryReading,
    PressureLevel,
    _process_tree,
    _sysctl_pressure_level,
    evaluate,
    guard_loop,
    read_memory,
    wait_for_recovery,
)

CONFIG = MemoryGuardConfig(
    max_model_bytes=8 * GIB,
    recovery_model_bytes=6 * GIB,
    interval_seconds=0.0,
    consecutive_readings=2,
    recovery_timeout_seconds=0.1,
)


def test_healthy_reading_does_not_trip():
    assert evaluate(MemoryReading(4 * GIB, PressureLevel.NORMAL), CONFIG) is None


def test_footprint_at_the_cap_requests_graceful_recovery():
    trip = evaluate(MemoryReading(8 * GIB, PressureLevel.NORMAL), CONFIG)
    assert trip is not None
    assert "LM Studio is holding" in trip.reason
    assert trip.restart_first is False


def test_footprint_below_the_cap_does_not_trip():
    assert evaluate(MemoryReading(7 * GIB, PressureLevel.WARNING), CONFIG) is None


def test_urgent_pressure_alone_does_not_trip():
    """Urgent is a system-wide state; only the model's own growth is tier one."""
    assert evaluate(MemoryReading(4 * GIB, PressureLevel.URGENT), CONFIG) is None


def test_critical_pressure_requests_restart_first():
    trip = evaluate(MemoryReading(4 * GIB, PressureLevel.CRITICAL), CONFIG)
    assert trip is not None
    assert trip.restart_first is True


def test_critical_pressure_precedes_footprint_reason():
    trip = evaluate(MemoryReading(9 * GIB, PressureLevel.CRITICAL), CONFIG)
    assert trip is not None
    assert trip.restart_first is True
    assert "critical" in trip.reason


def test_unmeasurable_platform_never_trips():
    assert evaluate(MemoryReading(), CONFIG) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", PressureLevel.NORMAL),
        ("1", PressureLevel.WARNING),
        ("2", PressureLevel.URGENT),
        ("3", PressureLevel.CRITICAL),
        ("4", PressureLevel.CRITICAL),
    ],
)
def test_sysctl_pressure_mapping(monkeypatch, raw, expected):
    monkeypatch.setattr("caal.memory_guard.sys.platform", "darwin")
    monkeypatch.setattr(
        "caal.memory_guard.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw),
    )
    assert _sysctl_pressure_level() == expected


def test_normal_pressure_survives_the_reading(monkeypatch):
    """NORMAL is zero: a healthy machine must still count as measured."""
    monkeypatch.setattr(
        "caal.memory_guard._sysctl_pressure_level", lambda: PressureLevel.NORMAL
    )
    monkeypatch.setattr("caal.memory_guard._model_footprint_bytes", lambda: None)

    def unavailable():  # pragma: no cover
        raise AssertionError("a readable sysctl level must not fall back")

    monkeypatch.setattr("caal.memory_guard._PRESSURE_SOURCE.current", unavailable)
    reading = read_memory()
    assert reading.pressure == PressureLevel.NORMAL
    assert reading.measured is True


def test_process_tree_matches_the_executable_not_the_command_line(monkeypatch):
    listing = "\n".join(
        [
            "  1     0 /sbin/launchd",
            " 25432     1 llmster",
            " 25438 25432 /Users/me/.lmstudio/.internal/utils/node",
            " 46380 25438 /Users/me/.lmstudio/.internal/utils/node",
            " 59525     1 /bin/zsh",  # a shell whose arguments mention llmster
            " 60001     1 llmsterctl",  # a different executable sharing a prefix
        ]
    )
    monkeypatch.setattr("caal.memory_guard.sys.platform", "darwin")
    monkeypatch.setattr(
        "caal.memory_guard.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=listing),
    )
    assert sorted(_process_tree("llmster")) == [25432, 25438, 46380]


@pytest.mark.asyncio
async def test_loop_debounces_the_footprint(monkeypatch):
    readings = [
        MemoryReading(1 * GIB),  # probe
        MemoryReading(9 * GIB),  # 1/2
        MemoryReading(1 * GIB),  # recovers, resets
        MemoryReading(9 * GIB),  # 1/2
        MemoryReading(int(8.5 * GIB)),  # 2/2 despite a changing reason string
    ]
    monkeypatch.setattr(
        "caal.memory_guard.read_memory",
        lambda: readings.pop(0) if readings else MemoryReading(9 * GIB),
    )
    tripped = []

    async def on_trip(trip):
        tripped.append(trip)

    await guard_loop(on_trip, CONFIG)
    assert len(tripped) == 1
    assert tripped[0].restart_first is False


@pytest.mark.asyncio
async def test_loop_acts_on_critical_pressure_immediately(monkeypatch):
    readings = [
        MemoryReading(1 * GIB, PressureLevel.NORMAL),
        MemoryReading(1 * GIB, PressureLevel.CRITICAL),
    ]
    monkeypatch.setattr("caal.memory_guard.read_memory", lambda: readings.pop(0))
    tripped = []

    async def on_trip(trip):
        tripped.append(trip)

    await guard_loop(on_trip, CONFIG)
    assert len(tripped) == 1
    assert tripped[0].restart_first is True


@pytest.mark.asyncio
async def test_recovery_requires_a_footprint_below_the_floor(monkeypatch):
    readings = [MemoryReading(7 * GIB), MemoryReading(5 * GIB)]
    monkeypatch.setattr("caal.memory_guard.read_memory", lambda: readings.pop(0))
    assert await wait_for_recovery(CONFIG) is True


@pytest.mark.asyncio
async def test_recovery_fails_while_the_footprint_stays_high(monkeypatch):
    monkeypatch.setattr(
        "caal.memory_guard.read_memory", lambda: MemoryReading(7 * GIB)
    )
    assert await wait_for_recovery(CONFIG) is False


@pytest.mark.asyncio
async def test_critical_pressure_aborts_graceful_recovery(monkeypatch):
    monkeypatch.setattr(
        "caal.memory_guard.read_memory",
        lambda: MemoryReading(1 * GIB, PressureLevel.CRITICAL),
    )
    assert await wait_for_recovery(CONFIG) is False


@pytest.mark.asyncio
async def test_loop_exits_when_nothing_is_measurable(monkeypatch):
    monkeypatch.setattr("caal.memory_guard.read_memory", lambda: MemoryReading())

    async def on_trip(trip):  # pragma: no cover
        raise AssertionError("tripped on a platform it cannot measure")

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=1)
