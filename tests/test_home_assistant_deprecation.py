from pathlib import Path

from caal.integrations.mcp_loader import load_mcp_config


def test_legacy_home_assistant_settings_do_not_load_tools(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("N8N_MCP_URL", raising=False)
    monkeypatch.delenv("N8N_MCP_TOKEN", raising=False)

    servers = load_mcp_config(
        {
            "hass_enabled": True,
            "hass_host": "http://homeassistant.local:8123",
            "hass_token": "legacy-token",
            "n8n_enabled": False,
        }
    )

    assert all(server.name != "home_assistant" for server in servers)


def test_json_home_assistant_server_is_ignored(monkeypatch, tmp_path):
    (tmp_path / "mcp_servers.json").write_text(
        '{"servers":[{"name":"home_assistant","url":"http://localhost:8123"}]}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("N8N_MCP_URL", raising=False)
    monkeypatch.delenv("N8N_MCP_TOKEN", raising=False)

    servers = load_mcp_config({"n8n_enabled": False})

    assert all(server.name != "home_assistant" for server in servers)


def test_prompts_do_not_advertise_home_assistant():
    prompt_dir = Path(__file__).parents[1] / "prompt"

    for prompt_path in prompt_dir.rglob("*.md"):
        prompt = prompt_path.read_text().lower()
        assert "hass(" not in prompt, prompt_path
