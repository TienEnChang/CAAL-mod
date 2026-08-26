"""Tests for the call-ending memory guard."""

import asyncio

import pytest

from caal.memory_guard import (
    GIB,
    MemoryGuardConfig,
    MemoryReading,
    evaluate,
    guard_loop,
)

CONFIG = MemoryGuardConfig(
    min_free_percent=10,
    max_swap_bytes=4 * GIB,
    max_swap_growth_bytes=1 * GIB,
    interval_seconds=0.0,
    consecutive_readings=3,
)


def test_healthy_reading_is_not_pressure():
    reading = MemoryReading(free_percent=69, swap_used_bytes=2 * GIB)
    assert evaluate(reading, CONFIG) is None


def test_low_free_memory_trips():
    reason = evaluate(MemoryReading(free_percent=4), CONFIG)
    assert reason is not None and "4%" in reason


def test_high_swap_trips():
    reason = evaluate(MemoryReading(swap_used_bytes=6 * GIB), CONFIG)
    assert reason is not None and "swap" in reason


def test_swap_growth_during_call_trips_below_absolute_limit():
    reason = evaluate(
        MemoryReading(swap_used_bytes=3 * GIB),
        CONFIG,
        baseline_swap_bytes=1 * GIB,
    )
    assert reason is not None and "grew" in reason


def test_retained_swap_at_call_start_does_not_trip_growth_limit():
    reading = MemoryReading(free_percent=50, swap_used_bytes=3 * GIB)
    assert evaluate(reading, CONFIG, baseline_swap_bytes=3 * GIB) is None


def test_boundary_is_not_pressure():
    """At exactly the limits nothing is wrong yet."""
    assert evaluate(MemoryReading(free_percent=10), CONFIG) is None
    assert evaluate(MemoryReading(swap_used_bytes=4 * GIB), CONFIG) is None


def test_unmeasurable_platform_never_trips():
    """Linux has neither signal; the guard must not guess."""
    assert evaluate(MemoryReading(), CONFIG) is None


@pytest.mark.asyncio
async def test_loop_ends_call_only_after_consecutive_pressure(monkeypatch):
    readings = [
        MemoryReading(free_percent=50),  # probe
        MemoryReading(free_percent=4),
        MemoryReading(free_percent=50),  # recovers, resets the streak
        MemoryReading(free_percent=4),
        MemoryReading(free_percent=4),
        MemoryReading(free_percent=4),  # third in a row -> end call
    ]
    def next_reading() -> MemoryReading:
        return readings.pop(0) if readings else MemoryReading(free_percent=4)

    monkeypatch.setattr("caal.memory_guard.read_memory", next_reading)

    ended: list[str] = []

    async def on_pressure(reason: str) -> None:
        ended.append(reason)

    await asyncio.wait_for(guard_loop(on_pressure, CONFIG), timeout=5)

    assert len(ended) == 1
    assert "4%" in ended[0]


@pytest.mark.asyncio
async def test_loop_exits_when_signals_unavailable(monkeypatch):
    monkeypatch.setattr("caal.memory_guard.read_memory", MemoryReading)

    async def on_pressure(reason: str) -> None:  # pragma: no cover
        raise AssertionError("must not end a call it cannot measure")

    await asyncio.wait_for(guard_loop(on_pressure, CONFIG), timeout=5)


@pytest.mark.asyncio
async def test_disabled_guard_does_nothing():
    async def on_pressure(reason: str) -> None:  # pragma: no cover
        raise AssertionError("guard was disabled")

    config = MemoryGuardConfig(enabled=False, interval_seconds=0.0)
    await asyncio.wait_for(guard_loop(on_pressure, config), timeout=5)
