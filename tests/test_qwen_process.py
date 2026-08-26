"""Tests for the direct local-Qwen restart helper."""

import io
from pathlib import Path

from caal.qwen_process import restart_local_qwen


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_remote_qwen_is_never_restarted(monkeypatch):
    called = False

    def run(*args, **kwargs):  # pragma: no cover
        nonlocal called
        called = True

    monkeypatch.setattr("caal.qwen_process.subprocess.run", run)
    assert restart_local_qwen(Path("/project"), "https://example.com/v1") is False
    assert called is False


def test_successful_restart_waits_for_memory_endpoint(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""

    commands = []
    monkeypatch.setattr(
        "caal.qwen_process.subprocess.run",
        lambda command, **kwargs: commands.append(command) or Result(),
    )
    monkeypatch.setattr(
        "caal.qwen_process.urllib.request.urlopen",
        lambda url, timeout: _Response(b'{"active_bytes": 1}'),
    )

    assert restart_local_qwen(Path("/project"), "http://localhost:8080/v1") is True
    assert commands == [["/project/start-native.sh", "--restart", "qwen"]]


def test_failed_restart_does_not_probe_readiness(monkeypatch):
    class Result:
        returncode = 1
        stderr = "failed"

    probed = False
    monkeypatch.setattr(
        "caal.qwen_process.subprocess.run", lambda command, **kwargs: Result()
    )

    def urlopen(*args, **kwargs):  # pragma: no cover
        nonlocal probed
        probed = True

    monkeypatch.setattr("caal.qwen_process.urllib.request.urlopen", urlopen)
    assert restart_local_qwen(Path("/project"), "http://localhost:8080/v1") is False
    assert probed is False
