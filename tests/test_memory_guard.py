"""The three trip signals, and what they mean.

They are not independent protections - they are three views of the same
failure, ordered by how early each can be seen. Only the MLX cap leads;
free-memory percentage lags because evicting pages raises it, and swap growth
trails by definition.
"""

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
    max_mlx_bytes=6 * GIB,
    min_free_percent=20,
    max_swap_growth_bytes=1 * GIB,
    interval_seconds=0.0,
    consecutive_readings=2,
)

HEALTHY = MemoryReading(free_percent=60, swap_used_bytes=2 * GIB, mlx_active_bytes=3 * GIB)


def test_healthy_reading_does_not_trip():
    assert evaluate(HEALTHY, CONFIG, baseline_swap_bytes=2 * GIB) is None


# --- signal 1: MLX pressure (leading) ---


def test_mlx_at_the_cap_trips():
    reading = MemoryReading(free_percent=60, mlx_active_bytes=6 * GIB)
    reason = evaluate(reading, CONFIG)
    assert reason is not None and "Qwen is holding" in reason


def test_mlx_below_the_cap_does_not_trip():
    assert evaluate(MemoryReading(mlx_active_bytes=5 * GIB), CONFIG) is None


def test_mlx_trips_before_the_system_notices():
    """The point of the MLX signal: it fires while headroom still looks fine."""
    reading = MemoryReading(free_percent=60, swap_used_bytes=0, mlx_active_bytes=7 * GIB)
    assert evaluate(reading, CONFIG, baseline_swap_bytes=0) is not None


# --- signal 2: system headroom (lagging) ---


def test_low_available_memory_trips():
    reason = evaluate(MemoryReading(free_percent=20), CONFIG)
    assert reason is not None and "20%" in reason


def test_available_memory_above_the_floor_does_not_trip():
    assert evaluate(MemoryReading(free_percent=21), CONFIG) is None


# --- signal 3: swap growth (trailing) ---


def test_swap_growth_trips():
    reading = MemoryReading(free_percent=60, swap_used_bytes=4 * GIB)
    reason = evaluate(reading, CONFIG, baseline_swap_bytes=3 * GIB)
    assert reason is not None and "swap grew" in reason


def test_absolute_swap_without_growth_does_not_trip():
    """macOS keeps swap long after the pressure; a stable machine is fine."""
    reading = MemoryReading(free_percent=60, swap_used_bytes=9 * GIB)
    assert evaluate(reading, CONFIG, baseline_swap_bytes=9 * GIB) is None


def test_swap_growth_needs_a_baseline():
    reading = MemoryReading(free_percent=60, swap_used_bytes=9 * GIB)
    assert evaluate(reading, CONFIG, baseline_swap_bytes=None) is None


# --- unmeasurable ---


def test_unmeasurable_platform_never_trips():
    """Linux has none of these signals; do not guess."""
    assert evaluate(MemoryReading(), CONFIG) is None


# --- the loop ---


@pytest.mark.asyncio
async def test_loop_trips_after_consecutive_readings(monkeypatch):
    readings = [
        MemoryReading(free_percent=60, mlx_active_bytes=1 * GIB),  # probe
        MemoryReading(free_percent=60, mlx_active_bytes=7 * GIB),  # 1/2
        MemoryReading(free_percent=60, mlx_active_bytes=1 * GIB),  # recovers, resets
        MemoryReading(free_percent=60, mlx_active_bytes=7 * GIB),  # 1/2
        MemoryReading(free_percent=60, mlx_active_bytes=7 * GIB),  # 2/2 -> trip
    ]

    def next_reading(url=None):
        return readings.pop(0) if readings else MemoryReading(mlx_active_bytes=7 * GIB)

    monkeypatch.setattr("caal.memory_guard.read_memory", next_reading)
    tripped = []

    async def on_trip(reason):
        tripped.append(reason)

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=5)
    assert len(tripped) == 1


@pytest.mark.asyncio
async def test_loop_exits_when_nothing_is_measurable(monkeypatch):
    monkeypatch.setattr("caal.memory_guard.read_memory", lambda url=None: MemoryReading())

    async def on_trip(reason):  # pragma: no cover
        raise AssertionError("tripped on a platform it cannot measure")

    await asyncio.wait_for(guard_loop(on_trip, CONFIG), timeout=5)


@pytest.mark.asyncio
async def test_disabled_guard_does_nothing():
    async def on_trip(reason):  # pragma: no cover
        raise AssertionError("guard was disabled")

    await asyncio.wait_for(
        guard_loop(on_trip, MemoryGuardConfig(enabled=False, interval_seconds=0.0)),
        timeout=5,
    )
