from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "native_memory_monitor.py"
SPEC = importlib.util.spec_from_file_location("native_memory_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def test_parse_swapusage() -> None:
    result = monitor.parse_swapusage(
        "vm.swapusage: total = 10240.00M used = 9822.12M free = 417.88M"
    )

    assert result["swap_total_bytes"] == 10 * 1024**3
    assert result["swap_used_bytes"] == int(9822.12 * 1024**2)


def test_parse_latest_prompt_cache() -> None:
    text = """
Prompt Cache: 2 sequences, 0.75 GB
Prompt Cache: 10 sequences, 3.99 GB
"""

    assert monitor.parse_prompt_cache(text) == {
        "sequences": 10,
        "bytes": int(3.99 * 1024**3),
    }


def test_parse_footprint_attributes_gpu_and_swap() -> None:
    payload = {
        "processes": [
            {
                "pid": 42,
                "footprint": 1000,
                "auxiliary": {"phys_footprint": 900, "phys_footprint_peak": 1200},
                "categories": {
                    "IOAccelerator (graphics)": {"dirty": 700, "swapped": 500},
                    "MALLOC_LARGE": {"dirty": 200, "swapped": 100},
                },
            }
        ]
    }

    result = monitor.parse_footprint(payload, {42: "qwen"})["qwen"]

    assert result == {
        "physical_bytes": 900,
        "physical_peak_bytes": 1200,
        "compressed_or_swapped_bytes": 600,
        "gpu_dirty_bytes": 700,
        "gpu_compressed_or_swapped_bytes": 500,
    }


def test_bad_swapusage_is_rejected() -> None:
    with pytest.raises(monitor.MonitorError):
        monitor.parse_swapusage("unavailable")
