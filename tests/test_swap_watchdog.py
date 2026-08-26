"""Qwen is restarted when swap grows past its per-run baseline.

Restarting is the only way to reclaim Qwen's memory: mlx-lm cannot cancel an
in-flight request, so by the time swap is climbing the allocation is already
committed. Growth is measured against a baseline rather than an absolute
ceiling because macOS retains swap long after the pressure that caused it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "native_memory_monitor.py"
SPEC = importlib.util.spec_from_file_location("native_memory_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

GIB = 1024**3


def watchdog(threshold_gib=1.0, cooldown=300):
    return monitor.SwapWatchdog(int(threshold_gib * GIB), cooldown)


def test_first_reading_only_sets_the_baseline():
    w = watchdog()
    assert w.should_restart(8 * GIB, now=0) is False
    assert w.baseline == 8 * GIB


def test_growth_past_threshold_restarts():
    w = watchdog()
    w.should_restart(2 * GIB, now=0)
    assert w.should_restart(int(3.5 * GIB), now=10) is True


def test_growth_below_threshold_is_left_alone():
    """Restarting mid-call is disruptive; 0.5 GiB is not worth it."""
    w = watchdog()
    w.should_restart(2 * GIB, now=0)
    assert w.should_restart(int(2.5 * GIB), now=10) is False


def test_growth_exactly_at_threshold_does_not_restart():
    w = watchdog()
    w.should_restart(2 * GIB, now=0)
    assert w.should_restart(3 * GIB, now=10) is False


def test_high_but_stable_swap_is_left_alone():
    """A machine that swapped earlier and has settled must not be restarted."""
    w = watchdog()
    w.should_restart(8 * GIB, now=0)
    assert w.should_restart(8 * GIB, now=60) is False
    assert w.should_restart(8 * GIB, now=120) is False


def test_released_swap_lowers_the_baseline():
    w = watchdog()
    w.should_restart(4 * GIB, now=0)
    w.should_restart(1 * GIB, now=10)      # drained
    assert w.baseline == 1 * GIB
    assert w.should_restart(int(1.8 * GIB), now=20) is False


def test_cooldown_blocks_a_restart_cascade():
    """Swap is released lazily, so the next reading can still look bad."""
    w = watchdog(cooldown=300)
    w.should_restart(2 * GIB, now=0)
    assert w.should_restart(4 * GIB, now=10) is True
    w.restarted(now=10)
    w.should_restart(4 * GIB, now=20)      # re-baselines
    assert w.should_restart(6 * GIB, now=30) is False


def test_restart_is_allowed_again_after_the_cooldown():
    w = watchdog(cooldown=300)
    w.should_restart(2 * GIB, now=0)
    w.restarted(now=0)
    w.should_restart(2 * GIB, now=400)     # re-baselines
    assert w.should_restart(4 * GIB, now=410) is True


def test_zero_threshold_disables_the_watchdog():
    w = watchdog(threshold_gib=0)
    assert w.enabled is False
    w.should_restart(2 * GIB, now=0)
    assert w.should_restart(99 * GIB, now=10) is False


def test_unreadable_swap_never_restarts():
    """If sysctl cannot be read, do not guess."""
    w = watchdog()
    assert w.should_restart(None, now=0) is False
    assert w.should_restart(None, now=10) is False
