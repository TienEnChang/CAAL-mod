from caal import settings


def test_configured_agent_name_overrides_generic_prompt_identity(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    (prompt_dir / "default.md").write_text("# CAAL\n\nYou are CAAL. {{CURRENT_DATE_CONTEXT}}")
    monkeypatch.setattr(settings, "PROMPT_DIR", prompt_dir)

    prompt = settings.load_prompt_with_context(
        timezone_id="UTC",
        timezone_display="UTC",
        agent_name="Cheese",
    )

    assert prompt.startswith("# Configured Assistant Identity")
    assert 'Your configured name is "Cheese".' in prompt
    assert "overrides generic product names such as CAAL or Cal" in prompt
    assert prompt.index('Your configured name is "Cheese".') < prompt.index("You are CAAL")


def test_configured_agent_name_is_normalized_to_one_line(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    (prompt_dir / "default.md").write_text("Assistant prompt")
    monkeypatch.setattr(settings, "PROMPT_DIR", prompt_dir)

    prompt = settings.load_prompt_with_context(agent_name="  Cheese\n  Cake  ")

    assert 'Your configured name is "Cheese Cake".' in prompt


def test_date_context_is_appended_after_the_stable_prompt(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    (prompt_dir / "default.md").write_text(
        "STABLE START {{CURRENT_DATE_CONTEXT}} STABLE END"
    )
    monkeypatch.setattr(settings, "PROMPT_DIR", prompt_dir)

    stable = settings.load_stable_prompt(
        timezone_display="UTC",
        agent_name="Cal",
    )
    populated = settings.load_prompt_with_context(
        timezone_id="UTC",
        timezone_display="UTC",
        agent_name="Cal",
    )

    assert "Today is" not in stable
    assert "STABLE START  STABLE END" in stable
    assert populated.startswith(stable)
    assert populated.index("STABLE END") < populated.index("# Current Session")
    assert populated.index("# Current Session") < populated.index("Today is")
