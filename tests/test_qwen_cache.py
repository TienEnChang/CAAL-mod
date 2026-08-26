from unittest.mock import Mock

from caal.qwen_cache import clear_local_qwen_cache


def test_clears_local_qwen_cache(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    post = Mock(return_value=response)
    monkeypatch.setattr("caal.qwen_cache.requests.post", post)

    assert clear_local_qwen_cache("http://127.0.0.1:8100/v1") is True
    post.assert_called_once_with("http://127.0.0.1:8100/v1/cache/clear", timeout=5.0)


def test_does_not_contact_remote_openai_compatible_server(monkeypatch):
    post = Mock()
    monkeypatch.setattr("caal.qwen_cache.requests.post", post)

    assert clear_local_qwen_cache("https://example.com/v1") is False
    post.assert_not_called()
