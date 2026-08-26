#!/usr/bin/env python3
"""Continuously attribute native CAAL memory, Metal, compression, and swap growth."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICE_MARKERS = {
    # Matches both the CAAL launcher wrapper (scripts/mlx_qwen_server.py)
    # and a stock "-m mlx_lm server" invocation. The pid file already
    # pins the process; these markers only guard against a reused pid.
    "qwen": ("mlx", "server"),
    "speech": ("local_speech_server.py",),
    "n8n": ("n8n/bin/n8n",),
    "livekit": ("livekit-server",),
    "agent": ("voice_agent.py", "start"),
    "frontend": ("next-server",),
}
# Absolute paths: services run with a PATH that omits /usr/sbin, so a bare
# "sysctl" raised FileNotFoundError - which _run does not catch - wiping out the
# whole system-metrics block and leaving every background sample empty.
MEMORY_PRESSURE_BIN = "/usr/bin/memory_pressure"
SYSCTL_BIN = "/usr/sbin/sysctl"
GIB = 1024**3
DEFAULT_SAMPLE_SECONDS = 60
# Last-resort backstop, deliberately far above the in-session guard's 1 GiB
# swap-growth trip. That guard polls every 2s, ends the session and releases the
# cache; this samples once a minute and restarts Qwen outright. Matching
# thresholds would mean both firing on one event, with this one arriving up to a
# minute late - likely during a fresh call the guard had already rescued. It
# should only act on pressure the session guard failed to contain, or that built
# up with no call running at all.
DEFAULT_SWAP_RESTART_GIB = 3.0
# Swap is released lazily, so the baseline cannot be trusted immediately after a
# restart; this keeps one restart from cascading into a loop.
DEFAULT_RESTART_COOLDOWN_SECONDS = 300
DEFAULT_DEEP_SECONDS = 300
DEFAULT_MAX_LOG_BYTES = 20 * 1024 * 1024
PROMPT_CACHE_RE = re.compile(
    r"Prompt Cache:\s+(?P<count>\d+) sequences,\s+"
    r"(?P<size>[0-9.]+)\s+(?P<unit>[KMGT]?B)",
    re.IGNORECASE,
)
SWAP_RE = re.compile(
    r"total\s*=\s*(?P<total>[0-9.]+)(?P<total_unit>[KMG])\s+"
    r"used\s*=\s*(?P<used>[0-9.]+)(?P<used_unit>[KMG])\s+"
    r"free\s*=\s*(?P<free>[0-9.]+)(?P<free_unit>[KMG])",
    re.IGNORECASE,
)
UNIT_BYTES = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
}


class MonitorError(RuntimeError):
    """A recoverable diagnostics error."""


def parse_swapusage(text: str) -> dict[str, int]:
    match = SWAP_RE.search(text)
    if not match:
        raise MonitorError("Could not parse vm.swapusage")
    result = {}
    for field in ("total", "used", "free"):
        value = float(match.group(field))
        unit = match.group(f"{field}_unit").upper()
        result[f"swap_{field}_bytes"] = int(value * UNIT_BYTES[unit])
    return result


def parse_prompt_cache(text: str) -> dict[str, int] | None:
    matches = list(PROMPT_CACHE_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    unit = match.group("unit").upper()
    return {
        "sequences": int(match.group("count")),
        "bytes": int(float(match.group("size")) * UNIT_BYTES[unit]),
    }


def parse_footprint(
    payload: dict[str, Any], pid_services: dict[int, str]
) -> dict[str, dict[str, int]]:
    services: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "physical_bytes": 0,
            "physical_peak_bytes": 0,
            "compressed_or_swapped_bytes": 0,
            "gpu_dirty_bytes": 0,
            "gpu_compressed_or_swapped_bytes": 0,
        }
    )
    for process in payload.get("processes", []):
        pid = int(process.get("pid", 0))
        service = pid_services.get(pid)
        if service is None:
            continue
        metrics = services[service]
        auxiliary = process.get("auxiliary", {})
        metrics["physical_bytes"] += int(
            auxiliary.get("phys_footprint", process.get("footprint", 0))
        )
        metrics["physical_peak_bytes"] += int(
            auxiliary.get("phys_footprint_peak", process.get("footprint", 0))
        )
        for category_name, category in process.get("categories", {}).items():
            swapped = int(category.get("swapped", 0))
            dirty = int(category.get("dirty", 0))
            metrics["compressed_or_swapped_bytes"] += swapped
            lowered = category_name.lower()
            if (
                "ioaccelerator" in lowered
                or "gpu" in lowered
                or "iosurface" in lowered
            ):
                metrics["gpu_dirty_bytes"] += dirty
                metrics["gpu_compressed_or_swapped_bytes"] += swapped
    return dict(services)


def _run(command: list[str], timeout: float = 30) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MonitorError(f"{' '.join(command[:2])} failed: {detail}")
    return result.stdout


def _process_table() -> dict[int, dict[str, Any]]:
    output = _run(["ps", "-axo", "pid=,ppid=,rss=,command="])
    processes = {}
    for raw_line in output.splitlines():
        parts = raw_line.strip().split(maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss_kib = map(int, parts[:3])
        except ValueError:
            continue
        processes[pid] = {
            "ppid": ppid,
            "rss_bytes": rss_kib * 1024,
            "command": parts[3],
        }
    return processes


def _service_processes(
    project: Path, processes: dict[int, dict[str, Any]]
) -> tuple[dict[int, str], dict[str, dict[str, Any]]]:
    pid_services: dict[int, str] = {}
    services: dict[str, dict[str, Any]] = {}
    children: dict[int, list[int]] = defaultdict(list)
    for pid, process in processes.items():
        children[process["ppid"]].append(pid)

    for service, markers in SERVICE_MARKERS.items():
        pid_file = project / ".native" / "pids" / f"{service}.pid"
        try:
            root_pid = int(pid_file.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            continue
        root = processes.get(root_pid)
        if root is None or not all(marker in root["command"] for marker in markers):
            continue
        pending = [root_pid]
        service_pids = []
        while pending:
            pid = pending.pop()
            if pid in pid_services:
                continue
            pid_services[pid] = service
            service_pids.append(pid)
            pending.extend(children.get(pid, []))
        services[service] = {
            "pids": sorted(service_pids),
            "rss_bytes": sum(processes[pid]["rss_bytes"] for pid in service_pids),
        }
    return pid_services, services


def _system_metrics() -> dict[str, int | float]:
    metrics: dict[str, int | float] = {}
    try:
        metrics.update(parse_swapusage(_run([SYSCTL_BIN, "vm.swapusage"])))
    except (MonitorError, subprocess.TimeoutExpired, OSError):
        pass
    try:
        pressure = _run([MEMORY_PRESSURE_BIN])
        match = re.search(r"System-wide memory free percentage:\s*(\d+)%", pressure)
        if match:
            metrics["memory_free_percent"] = int(match.group(1))
    except (MonitorError, subprocess.TimeoutExpired, OSError):
        pass
    return metrics


def _latest_prompt_cache(project: Path) -> dict[str, int] | None:
    path = project / ".native" / "logs" / "qwen.log"
    try:
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 256 * 1024))
            return parse_prompt_cache(source.read().decode(errors="replace"))
    except OSError:
        return None


def _deep_footprint(pid_services: dict[int, str]) -> dict[str, dict[str, int]]:
    if not pid_services:
        return {}
    descriptor, temporary_name = tempfile.mkstemp(prefix="caal-footprint-", suffix=".json")
    os.close(descriptor)
    temporary = Path(temporary_name)
    command = ["footprint"]
    for pid in sorted(pid_services):
        command.extend(["-p", str(pid)])
    command.extend(["-j", str(temporary), "-f", "bytes", "--swapped", "--wired"])
    try:
        _run(command, timeout=60)
        return parse_footprint(json.loads(temporary.read_text()), pid_services)
    finally:
        temporary.unlink(missing_ok=True)


def collect_sample(project: Path, *, deep: bool) -> dict[str, Any]:
    processes = _process_table()
    pid_services, services = _service_processes(project, processes)
    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "deep": deep,
        "system": _system_metrics(),
        "services": services,
    }
    if prompt_cache := _latest_prompt_cache(project):
        record["qwen_prompt_cache"] = prompt_cache
    if deep:
        footprints = _deep_footprint(pid_services)
        for service, metrics in footprints.items():
            services.setdefault(service, {}).update(metrics)
    record["totals"] = {
        key: sum(int(metrics.get(key, 0)) for metrics in services.values())
        for key in (
            "rss_bytes",
            "physical_bytes",
            "physical_peak_bytes",
            "compressed_or_swapped_bytes",
            "gpu_dirty_bytes",
            "gpu_compressed_or_swapped_bytes",
        )
    }
    return record


def _rotate(path: Path, max_bytes: int) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    rotated.unlink(missing_ok=True)
    os.replace(path, rotated)


def _append_record(path: Path, record: dict[str, Any], max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path, max_bytes)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, separators=(",", ":")) + "\n")


class SwapWatchdog:
    """Decides when Qwen should be restarted to reclaim its memory.

    Growth is measured against a baseline rather than an absolute ceiling:
    macOS retains swap long after the pressure that caused it, so an absolute
    limit would fire forever on a machine that swapped once.
    """

    def __init__(self, threshold_bytes: int, cooldown_seconds: int) -> None:
        self.threshold_bytes = threshold_bytes
        self.cooldown_seconds = cooldown_seconds
        self.baseline: int | None = None
        self.last_restart: float | None = None

    @property
    def enabled(self) -> bool:
        return self.threshold_bytes > 0

    def growth(self, swap_used: int) -> int:
        if self.baseline is None:
            return 0
        return swap_used - self.baseline

    def should_restart(self, swap_used: int | None, now: float) -> bool:
        if not self.enabled or swap_used is None:
            return False
        if self.baseline is None:
            self.baseline = swap_used
            return False
        growth = swap_used - self.baseline
        if growth < 0:
            # Swap was released; track the new floor.
            self.baseline = swap_used
            return False
        if growth <= self.threshold_bytes:
            return False
        if self.last_restart is not None and now - self.last_restart < self.cooldown_seconds:
            return False
        return True

    def restarted(self, now: float) -> None:
        self.last_restart = now
        # Swap drains lazily, so re-baseline from the next reading rather than
        # measuring against a floor that no longer reflects reality.
        self.baseline = None


def _restart_qwen(project: Path) -> bool:
    """Restart the Qwen service, returning whether it succeeded."""
    script = project / "start-native.sh"
    if not script.exists():
        logging.error("Cannot restart qwen: %s is missing", script)
        return False
    try:
        result = subprocess.run(
            [str(script), "--restart", "qwen"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logging.error("Restarting qwen failed: %s", error)
        return False
    if result.returncode != 0:
        logging.error(
            "Restarting qwen exited %s: %s",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return False
    return True


def monitor(
    project: Path,
    log_path: Path,
    sample_seconds: int,
    deep_seconds: int,
    max_log_bytes: int,
    swap_restart_bytes: int = 0,
    restart_cooldown: int = DEFAULT_RESTART_COOLDOWN_SECONDS,
) -> None:
    stopping = False
    watchdog = SwapWatchdog(swap_restart_bytes, restart_cooldown)

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    last_deep = 0.0
    while not stopping:
        started = time.monotonic()
        deep = started - last_deep >= deep_seconds
        try:
            record = collect_sample(project, deep=deep)
            # Retry a deep sample next minute if startup has not created services yet.
            if deep and record["services"]:
                last_deep = started
            _append_record(log_path, record, max_log_bytes)

            swap_used = record.get("system", {}).get("swap_used_bytes")
            if watchdog.should_restart(swap_used, started):
                growth = watchdog.growth(swap_used)
                logging.warning(
                    "Swap grew %.2f GiB (limit %.2f GiB) - restarting qwen",
                    growth / GIB,
                    swap_restart_bytes / GIB,
                )
                record["swap_restart"] = {
                    "growth_bytes": growth,
                    "restarted": _restart_qwen(project),
                }
                _append_record(log_path, record, max_log_bytes)
                watchdog.restarted(time.monotonic())
        except Exception as exc:
            _append_record(
                log_path,
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "deep": deep,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                max_log_bytes,
            )
        remaining = max(0.0, sample_seconds - (time.monotonic() - started))
        deadline = time.monotonic() + remaining
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


def _load_records(path: Path, hours: float) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    records = []
    for candidate in (path.with_suffix(path.suffix + ".1"), path):
        try:
            lines = candidate.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
                timestamp = datetime.fromisoformat(record["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            if timestamp >= cutoff:
                records.append(record)
    return sorted(records, key=lambda record: record["timestamp"])


def _gib(value: int | float | None) -> str:
    return f"{float(value or 0) / 1024**3:.2f} GiB"


def _local_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds")


def report(log_path: Path, hours: float) -> None:
    all_records = _load_records(log_path, hours)
    deep_records = [
        record
        for record in all_records
        if record.get("deep") and record.get("services")
    ]
    if not deep_records:
        raise MonitorError(f"No deep memory samples in the last {hours:g} hours")
    first, latest = deep_records[0], deep_records[-1]
    print(f"CAAL memory report — last {hours:g} hours ({len(deep_records)} deep samples)")
    print(f"First sample:  {_local_time(first['timestamp'])}")
    print(f"Latest sample: {_local_time(latest['timestamp'])}")
    print()
    print("Component         First      Latest       Peak     Growth")
    service_names = sorted(
        {name for record in deep_records for name in record.get("services", {})}
    )
    growth_by_service = {}
    for name in service_names:
        values = [
            int(record.get("services", {}).get(name, {}).get("physical_bytes", 0))
            for record in deep_records
        ]
        first_value, latest_value, peak_value = values[0], values[-1], max(values)
        growth_by_service[name] = latest_value - first_value
        print(
            f"{name:<14} {_gib(first_value):>9} {_gib(latest_value):>11} "
            f"{_gib(peak_value):>10} {_gib(latest_value - first_value):>10}"
        )
    total_growth = latest["totals"].get("physical_bytes", 0) - first["totals"].get(
        "physical_bytes", 0
    )
    print(
        f"{'TOTAL':<14} {_gib(first['totals'].get('physical_bytes')):>9} "
        f"{_gib(latest['totals'].get('physical_bytes')):>11} "
        f"{_gib(max(record['totals'].get('physical_bytes', 0) for record in deep_records)):>10} "
        f"{_gib(total_growth):>10}"
    )
    print()
    system_records = [record for record in all_records if record.get("system")]
    swap_values = [record["system"].get("swap_used_bytes", 0) for record in system_records]
    if swap_values:
        print(
            "System swap: "
            f"{_gib(swap_values[0])} → {_gib(swap_values[-1])}; "
            f"peak {_gib(max(swap_values))}"
        )
    cache_values = [
        record.get("qwen_prompt_cache", {}).get("bytes", 0) for record in all_records
    ]
    cache_counts = [
        record.get("qwen_prompt_cache", {}).get("sequences", 0) for record in all_records
    ]
    if cache_values:
        print(
            f"Qwen prompt cache peak: {_gib(max(cache_values))} "
            f"({max(cache_counts)} sequences)"
        )
    gpu_peak = max(
        record["totals"].get("gpu_dirty_bytes", 0) for record in deep_records
    )
    compressed_peak = max(
        record["totals"].get("compressed_or_swapped_bytes", 0)
        for record in deep_records
    )
    print(f"CAAL GPU/Metal dirty peak: {_gib(gpu_peak)}")
    print(f"CAAL compressed/swapped attribution peak: {_gib(compressed_peak)}")
    if growth_by_service:
        driver = max(growth_by_service, key=growth_by_service.get)
        print(f"Largest current growth driver: {driver} ({_gib(growth_by_service[driver])})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--log", type=Path, help="JSONL log path (default: .native/logs/memory.jsonl)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    monitor_parser = subparsers.add_parser("monitor")
    monitor_parser.add_argument(
        "--sample-seconds", type=int, default=DEFAULT_SAMPLE_SECONDS
    )
    monitor_parser.add_argument("--deep-seconds", type=int, default=DEFAULT_DEEP_SECONDS)
    monitor_parser.add_argument("--max-log-bytes", type=int, default=DEFAULT_MAX_LOG_BYTES)
    monitor_parser.add_argument(
        "--swap-restart-gb",
        type=float,
        default=DEFAULT_SWAP_RESTART_GIB,
        help=(
            "Backstop: restart qwen once swap grows this far above its baseline "
            "(0 disables). Well above the in-session guard's own trip."
        ),
    )
    monitor_parser.add_argument(
        "--restart-cooldown", type=int, default=DEFAULT_RESTART_COOLDOWN_SECONDS
    )
    subparsers.add_parser("sample")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--hours", type=float, default=24)
    return parser


def main() -> int:
    args = _parser().parse_args()
    project = args.project.resolve()
    log_path = (args.log or project / ".native" / "logs" / "memory.jsonl").resolve()
    try:
        if args.command == "monitor":
            monitor(
                project,
                log_path,
                max(5, args.sample_seconds),
                max(30, args.deep_seconds),
                max(1024 * 1024, args.max_log_bytes),
                swap_restart_bytes=int(max(0.0, args.swap_restart_gb) * GIB),
                restart_cooldown=max(0, args.restart_cooldown),
            )
        elif args.command == "sample":
            record = collect_sample(project, deep=True)
            _append_record(log_path, record, DEFAULT_MAX_LOG_BYTES)
            print(json.dumps(record, indent=2))
        else:
            report(log_path, args.hours)
    except (MonitorError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
