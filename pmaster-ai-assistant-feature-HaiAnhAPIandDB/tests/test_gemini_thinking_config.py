from gemini_client import thinking_config_for


def test_gemini_3_uses_thinking_level():
    config = thinking_config_for("gemini-3.5-flash-lite", "minimal")
    assert config.thinking_level is not None and config.thinking_budget is None


def test_gemini_25_flash_uses_budget_not_level():
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        config = thinking_config_for(model, "minimal")
        assert config.thinking_budget == 0 and config.thinking_level is None


def test_other_models_send_no_thinking_config():
    assert thinking_config_for("gemini-2.5-pro") is None
    assert thinking_config_for("gemini-2.0-flash") is None
