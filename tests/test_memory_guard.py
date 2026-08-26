"""Tests for the MLX and macOS-pressure memory guard."""

import asyncio

import pytest

from caal.memory_guard import (
    GIB,
    MemoryGuardConfig,
    MemoryReading,
    PressureLevel,
    evaluate,
    guard_loop,
    wait_for_recovery,
)

CONFIG = MemoryGuardConfig(
    max_mlx_bytes=6 * GIB,
    recovery_mlx_bytes=int(4.5 * GIB),
    interval_seconds=0.0,
    consecutive_readings=2,
    recovery_timeout_seconds=0.1,
)


def test_healthy_reading_does_not_trip():
    assert evaluate(MemoryReading(3 * GIB, PressureLevel.NORMAL), CONFIG) is None


def test_mlx_at_the_cap_requests_graceful_recovery():
    trip = evaluate(MemoryReading(6 * GIB, PressureLevel.NORMAL), CONFIG)
    assert trip is not None
    assert "Qwen is holding" in trip.reason
    assert trip.restart_first is False


def test_mlx_below_the_cap_does_not_trip():
    assert evaluate(MemoryReading(5 * GIB, PressureLevel.WARNING), CONFIG) is None


def test_critical_pressure_requests_restart_first():
    trip = evaluate(MemoryReading(3 * GIB, PressureLevel.CRITICAL), CONFIG)
    assert trip is not None
    assert trip.restart_first is True


def test_critical_pressure_precedes_mlx_reason():
    trip = evaluate(MemoryReading(7 * GIB, PressureLevel.CRITICAL), CONFIG)
    assert trip is not None
    assert trip.restart_first is True
    assert "critical" in trip.reason


def test_unmeasurable_platform_never_trips():
    assert evaluate(MemoryReading(), CONFIG) is None


@pytest.mark.asyncio
async def test_loop_debounces_mlx_allocation(monkeypatch):
    readings = [
        MemoryReading(1 * GIB),  # probe
        MemoryReading(7 * GIB),  # 1/2
        MemoryReading(1 * GIB),  # recovers, resets
        MemoryReading(7 * GIB),  # 1/2
        MemoryReading(int(6.5 * GIB)),  # 2/2 despite a changing reason string
    ]

    monkeypatch.setattr(
        "caal.memory_guard.read_memory",
        lambda url=None: readings.pop(0) if readings else MemoryReading(7 * GIB),
    )
    tripped = []

    async def on_trip(trip):
        tripped.append(trip)

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=5)
    assert len(tripped) == 1
    assert tripped[0].restart_first is False


@pytest.mark.asyncio
async def test_loop_acts_on_critical_pressure_immediately(monkeypatch):
    readings = [
        MemoryReading(1 * GIB, PressureLevel.NORMAL),
        MemoryReading(1 * GIB, PressureLevel.CRITICAL),
    ]
    monkeypatch.setattr(
        "caal.memory_guard.read_memory", lambda url=None: readings.pop(0)
    )
    tripped = []

    async def on_trip(trip):
        tripped.append(trip)

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=5)
    assert len(tripped) == 1
    assert tripped[0].restart_first is True


@pytest.mark.asyncio
async def test_recovery_requires_readable_mlx_below_floor(monkeypatch):
    readings = [MemoryReading(), MemoryReading(4 * GIB)]
    monkeypatch.setattr(
        "caal.memory_guard.read_memory", lambda url=None: readings.pop(0)
    )
    assert await wait_for_recovery(CONFIG, "http://127.0.0.1:8080/v1") is True


@pytest.mark.asyncio
async def test_critical_pressure_aborts_graceful_recovery(monkeypatch):
    monkeypatch.setattr(
        "caal.memory_guard.read_memory",
        lambda url=None: MemoryReading(4 * GIB, PressureLevel.CRITICAL),
    )
    assert await wait_for_recovery(CONFIG, "http://127.0.0.1:8080/v1") is False


@pytest.mark.asyncio
async def test_loop_exits_when_nothing_is_measurable(monkeypatch):
    monkeypatch.setattr("caal.memory_guard.read_memory", lambda url=None: MemoryReading())

    async def on_trip(trip):  # pragma: no cover
        raise AssertionError("tripped on a platform it cannot measure")

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=5)


@pytest.mark.asyncio
async def test_disabled_guard_does_nothing():
    async def on_trip(trip):  # pragma: no cover
        raise AssertionError("guard was disabled")

    await asyncio.wait_for(
        guard_loop(on_trip, MemoryGuardConfig(enabled=False, interval_seconds=0.0)),
        timeout=5,
    )
