"""Tests for completed-call MLX speech cache cleanup."""

from unittest.mock import Mock, patch

from caal.speech_cache import clear_local_speech_cache


@patch("caal.speech_cache.requests.post")
def test_local_speech_cache_is_cleared(post):
    post.return_value = Mock()

    assert clear_local_speech_cache("http://127.0.0.1:8001/v1") is True
    post.assert_called_once_with("http://127.0.0.1:8001/v1/cache/clear", timeout=10.0)
    post.return_value.raise_for_status.assert_called_once_with()


@patch("caal.speech_cache.requests.post")
def test_remote_speech_service_is_left_alone(post):
    assert clear_local_speech_cache("https://example.com/v1") is False
    post.assert_not_called()
