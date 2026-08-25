"""Wiring between n8n config and the webhook base URL CAAL calls."""

import pytest

from caal.integrations.mcp_loader import load_mcp_config
from caal.integrations.n8n import mcp_url_to_base_url

LOCAL_MCP_URL = "http://127.0.0.1:5678/mcp-server/http"


@pytest.mark.parametrize(
    ("mcp_url", "expected"),
    [
        (LOCAL_MCP_URL, "http://127.0.0.1:5678"),
        ("http://192.168.1.100:5678/mcp-server/http", "http://192.168.1.100:5678"),
        ("http://127.0.0.1:5678/mcp-server/sse", "http://127.0.0.1:5678"),
        ("http://127.0.0.1:5678/mcp-server", "http://127.0.0.1:5678"),
        # Trailing slashes must not leave an empty final segment behind.
        (LOCAL_MCP_URL + "/", "http://127.0.0.1:5678"),
        # An instance under a path prefix keeps that prefix.
        (
            "https://example.com/n8n/mcp-server/http",
            "https://example.com/n8n",
        ),
        # A bare host is already the base URL.
        ("http://127.0.0.1:5678", "http://127.0.0.1:5678"),
    ],
)
def test_mcp_url_to_base_url(mcp_url, expected):
    assert mcp_url_to_base_url(mcp_url) == expected


def _n8n_server(configs):
    return next((c for c in configs if c.name == "n8n"), None)


def test_env_supplies_n8n_when_settings_leave_url_blank(monkeypatch):
    """The native launcher exports the URL while .env holds the token."""
    monkeypatch.setenv("N8N_MCP_URL", LOCAL_MCP_URL)
    monkeypatch.setenv("N8N_MCP_TOKEN", "token-from-env")

    server = _n8n_server(
        load_mcp_config({"n8n_enabled": True, "n8n_url": "", "n8n_token": ""})
    )

    assert server is not None
    assert server.url == LOCAL_MCP_URL
    assert server.auth_token == "token-from-env"
    assert mcp_url_to_base_url(server.url) == "http://127.0.0.1:5678"


def test_settings_url_takes_priority_over_env(monkeypatch):
    monkeypatch.setenv("N8N_MCP_URL", LOCAL_MCP_URL)
    monkeypatch.setenv("N8N_MCP_TOKEN", "token-from-env")

    server = _n8n_server(
        load_mcp_config(
            {
                "n8n_enabled": True,
                "n8n_url": "http://10.0.0.5:5678/mcp-server/http",
                "n8n_token": "token-from-settings",
            }
        )
    )

    assert server is not None
    assert server.url == "http://10.0.0.5:5678/mcp-server/http"
    assert server.auth_token == "token-from-settings"


def test_disabled_settings_beat_env(monkeypatch):
    """n8n_enabled=False is a hard off switch, even with env vars set."""
    monkeypatch.setenv("N8N_MCP_URL", LOCAL_MCP_URL)
    monkeypatch.setenv("N8N_MCP_TOKEN", "token-from-env")

    assert _n8n_server(load_mcp_config({"n8n_enabled": False})) is None
