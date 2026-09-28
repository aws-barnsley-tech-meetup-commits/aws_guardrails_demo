"""Model id resolution. No AWS calls."""

from __future__ import annotations

import pytest

from c1 import config
from c1.config import (
    BOT_MODEL_ID_VAR,
    JUDGE_MODEL_ID_VAR,
    ModelNotConfigured,
    bot_model_id,
    judge_model_id,
    require_model_id,
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    for name in (
        BOT_MODEL_ID_VAR,
        config.BOT_MODEL_ID_PREMIUM_VAR,
        JUDGE_MODEL_ID_VAR,
        config.GUARDRAIL_ID_VAR,
        config.GUARDRAIL_VERSION_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# --- resolution ------------------------------------------------------------


def test_bot_model_id_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "global.anthropic.claude-sonnet-5")
    assert bot_model_id() == "global.anthropic.claude-sonnet-5"


def test_judge_model_id_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(JUDGE_MODEL_ID_VAR, "deepseek.v3-v1:0")
    assert judge_model_id() == "deepseek.v3-v1:0"


def test_the_two_variables_are_independent(monkeypatch):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "bot-model")
    monkeypatch.setenv(JUDGE_MODEL_ID_VAR, "judge-model")
    assert bot_model_id() == "bot-model"
    assert judge_model_id() == "judge-model"


def test_surrounding_whitespace_is_trimmed(monkeypatch):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "  global.anthropic.claude-sonnet-5\n")
    assert bot_model_id() == "global.anthropic.claude-sonnet-5"


# --- no silent defaults ----------------------------------------------------


@pytest.mark.parametrize("resolver", [bot_model_id, judge_model_id])
def test_a_missing_variable_raises_rather_than_defaulting(resolver):
    with pytest.raises(ModelNotConfigured):
        resolver()


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_a_blank_variable_raises(monkeypatch, value):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, value)
    with pytest.raises(ModelNotConfigured):
        bot_model_id()


def test_the_error_names_the_variable_and_suggests_a_fix():
    with pytest.raises(ModelNotConfigured) as excinfo:
        require_model_id("SOME_MODEL_VAR")
    message = str(excinfo.value)
    assert "SOME_MODEL_VAR" in message
    assert ".env" in message or "environment" in message


def test_no_model_id_is_hardcoded_in_the_module():
    """A literal model id here would reintroduce the silent-default bug.

    The example in the error message is the one allowed exception, so this
    checks the resolver never returns anything without the environment.
    """
    source = (config.repo_root() / "c1" / "config.py").read_text(encoding="utf-8")
    code_lines = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#") and "claude" in line
    ]
    # Only the illustrative line inside the raised message may mention a model.
    assert len(code_lines) <= 1, code_lines


# --- repo layout discovery -------------------------------------------------


def test_repo_root_is_found_from_a_checkout():
    root = config.repo_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert (root / "c1").is_dir()


def test_env_file_points_at_the_repo_root_when_present():
    found = config.env_file()
    if found is not None:
        assert found == config.repo_root() / ".env"


def test_load_env_file_is_idempotent():
    """Called at import and possibly again by an entry point."""
    assert config.load_env_file() == config.load_env_file()


# --- model tiers -----------------------------------------------------------


def test_high_volume_tier_uses_bot_model_id(monkeypatch):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "cheap-model")
    assert config.model_id_for_tier(config.TIER_HIGH_VOLUME) == "cheap-model"


def test_frontier_tier_uses_the_premium_variable(monkeypatch):
    monkeypatch.setenv(config.BOT_MODEL_ID_PREMIUM_VAR, "expensive-model")
    assert config.model_id_for_tier(config.TIER_FRONTIER) == "expensive-model"


def test_the_two_tiers_are_independent(monkeypatch):
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "cheap-model")
    monkeypatch.setenv(config.BOT_MODEL_ID_PREMIUM_VAR, "expensive-model")
    assert config.bot_model_id() == "cheap-model"
    assert config.bot_model_id_premium() == "expensive-model"


def test_frontier_tier_with_no_premium_model_raises(monkeypatch):
    monkeypatch.delenv(config.BOT_MODEL_ID_PREMIUM_VAR, raising=False)
    monkeypatch.setenv(BOT_MODEL_ID_VAR, "cheap-model")
    with pytest.raises(ModelNotConfigured):
        config.model_id_for_tier(config.TIER_FRONTIER)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_tier_header_defaults_to_high_volume(value):
    assert config.normalise_model_tier(value) == config.DEFAULT_MODEL_TIER
    assert config.DEFAULT_MODEL_TIER == config.TIER_HIGH_VOLUME


@pytest.mark.parametrize(
    ("value", "expected"),
    [("frontier", "frontier"), ("FRONTIER", "frontier"), ("  frontier  ", "frontier"),
     ("high_volume", "high_volume")],
)
def test_tier_header_is_normalised(value, expected):
    assert config.normalise_model_tier(value) == expected


@pytest.mark.parametrize("value", ["premium", "cheap", "frontier-tier", "true", "1"])
def test_unknown_tier_raises_rather_than_falling_back(value):
    """Silently serving the cheap model while the UI says frontier would lie."""
    with pytest.raises(config.UnknownModelTier):
        config.normalise_model_tier(value)


def test_model_id_for_an_unknown_tier_raises():
    with pytest.raises(config.UnknownModelTier):
        config.model_id_for_tier("nonsense")


# --- guardrail state -------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  "])
def test_absent_guardrail_header_defaults_to_off(value):
    assert config.normalise_guardrail_state(value) == config.DEFAULT_GUARDRAIL_STATE
    assert config.DEFAULT_GUARDRAIL_STATE == config.GUARDRAIL_OFF


@pytest.mark.parametrize(("value", "expected"), [("on", "on"), ("ON", "on"), (" Off ", "off")])
def test_guardrail_header_is_normalised(value, expected):
    assert config.normalise_guardrail_state(value) == expected


@pytest.mark.parametrize("value", ["yes", "true", "enabled", "1"])
def test_unknown_guardrail_state_raises(value):
    """Treating a typo as off would show an unguarded answer labelled guarded."""
    with pytest.raises(ValueError):
        config.normalise_guardrail_state(value)


# --- guardrail identity ----------------------------------------------------


def test_guardrail_values_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(config.GUARDRAIL_ID_VAR, "gr-123")
    monkeypatch.setenv(config.GUARDRAIL_VERSION_VAR, "DRAFT")
    assert config.guardrail_id() == "gr-123"
    assert config.guardrail_version() == "DRAFT"
    assert config.guardrail_configured() is True


@pytest.mark.parametrize("missing", ["GUARDRAIL_ID_VAR", "GUARDRAIL_VERSION_VAR"])
def test_guardrail_not_configured_when_either_is_missing(monkeypatch, missing):
    monkeypatch.setenv(config.GUARDRAIL_ID_VAR, "gr-123")
    monkeypatch.setenv(config.GUARDRAIL_VERSION_VAR, "DRAFT")
    monkeypatch.delenv(getattr(config, missing), raising=False)
    assert config.guardrail_configured() is False


def test_missing_guardrail_id_raises(monkeypatch):
    monkeypatch.delenv(config.GUARDRAIL_ID_VAR, raising=False)
    with pytest.raises(config.GuardrailNotConfigured):
        config.guardrail_id()


def test_guardrail_values_are_trimmed(monkeypatch):
    monkeypatch.setenv(config.GUARDRAIL_VERSION_VAR, "  DRAFT\n")
    assert config.guardrail_version() == "DRAFT"
