"""Tests for releasing memory held by CAAL's local MLX model server."""

from unittest.mock import Mock, patch

from caal.model_cache import (
    clear_local_model_cache,
    drain_local_model_batch,
    local_model_root,
    read_local_model_memory,
    restart_local_model_server,
    unload_local_model,
)


def test_local_root_strips_the_version_suffix():
    assert local_model_root("http://127.0.0.1:8100/v1") == "http://127.0.0.1:8100"
    assert local_model_root("http://localhost:8100") == "http://localhost:8100"


def test_remote_endpoint_has_no_root():
    assert local_model_root("https://api.example.com/v1") is None


@patch("caal.model_cache.requests.post")
def test_prompt_cache_is_cleared_without_unloading(post):
    post.return_value = Mock()

    assert clear_local_model_cache("http://127.0.0.1:8100/v1") is True
    post.assert_called_once_with("http://127.0.0.1:8100/v1/cache/clear", timeout=5.0)
    post.return_value.raise_for_status.assert_called_once_with()


@patch("caal.model_cache.requests.post")
def test_remote_model_is_left_alone(post):
    """Another machine's cache is not ours to clear."""
    assert clear_local_model_cache("https://api.example.com/v1") is False
    assert unload_local_model("https://api.example.com/v1") is False
    post.assert_not_called()


@patch("caal.model_cache.requests.post")
def test_unload_reports_failure_rather_than_raising(post):
    import requests

    post.side_effect = requests.RequestException("connection refused")
    assert unload_local_model("http://127.0.0.1:8100/v1") is False


@patch("caal.model_cache.requests.get")
def test_memory_counters_are_read_from_the_local_server(get):
    get.return_value = Mock()
    get.return_value.json.return_value = {"active_bytes": 42}

    assert read_local_model_memory("http://127.0.0.1:8100/v1") == {"active_bytes": 42}
    get.assert_called_once_with("http://127.0.0.1:8100/v1/memory", timeout=3.0)


@patch("caal.model_cache.requests.get")
def test_unreadable_memory_is_absent_rather_than_zero(get):
    """A missing reading must not look like a model holding no memory."""
    import requests

    get.side_effect = requests.RequestException("refused")
    assert read_local_model_memory("http://127.0.0.1:8100/v1") is None


@patch("caal.model_cache.requests.get")
@patch("caal.model_cache.subprocess.run")
def test_hard_reset_restarts_the_supervised_process(run, get, tmp_path):
    script = tmp_path / "start-native.sh"
    script.touch()
    run.return_value = Mock(returncode=0, stdout="restarted", stderr="")
    get.return_value = Mock()

    assert restart_local_model_server(
        tmp_path,
        "http://127.0.0.1:8100/v1",
    )
    run.assert_called_once_with(
        [str(script), "--restart", "model"],
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    get.assert_called_once_with("http://127.0.0.1:8100/v1/models", timeout=1)


@patch("caal.model_cache.requests.post")
def test_drain_spends_exactly_one_token(post):
    """The drain exists to reallocate the batch, not to produce output."""
    post.return_value = Mock()

    assert drain_local_model_batch("http://127.0.0.1:8100/v1", "org/model") is True
    body = post.call_args.kwargs["json"]
    assert body["model"] == "org/model"
    assert body["max_tokens"] == 1
    assert body["temperature"] == 0


@patch("caal.model_cache.requests.post")
def test_remote_model_is_not_drained(post):
    assert drain_local_model_batch("https://api.example.com/v1", "org/model") is False
    post.assert_not_called()


@patch("caal.model_cache.requests.post")
def test_a_failed_drain_does_not_abort_teardown(post):
    import requests

    post.side_effect = requests.RequestException("refused")
    assert drain_local_model_batch("http://127.0.0.1:8100/v1", "org/model") is False
