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

    result = monitor.parse_footprint(payload, {42: "model"})["model"]

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


def test_model_lane_tracks_caals_own_server(monkeypatch, tmp_path: Path) -> None:
    """The model lane is one supervised process now, not a foreign daemon tree.

    Serving through mlx-lm in CAAL's own process means the footprint measures
    only CAAL's model. Under LM Studio the llmster tree had to be walked, and
    any other model loaded in that runtime was counted here too.
    """
    pid_dir = tmp_path / ".native" / "pids"
    pid_dir.mkdir(parents=True)
    (pid_dir / "model.pid").write_text("46349")
    processes = {
        46349: {
            "ppid": 1,
            "rss_bytes": 4000,
            "command": "python /app/scripts/mlx_model_server.py --port 8100",
        },
        25432: {"ppid": 1, "rss_bytes": 300, "command": "llmster"},
        59525: {"ppid": 1, "rss_bytes": 7, "command": "zsh -c grep mlx_model_server.py"},
    }

    _, services = monitor._service_processes(tmp_path, processes)

    assert services["model"]["pids"] == [46349]
    assert services["model"]["rss_bytes"] == 4000
