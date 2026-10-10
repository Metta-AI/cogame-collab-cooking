"""Player policy registration preserves raw strategy and validates declared controls."""

import pytest
from pydantic import ValidationError

from collab_cooking.coworld.plan_protocol import PlayerConfiguration
from collab_cooking.coworld.player import registration
from tests.harness import build_episode


def test_native_strategy_whitespace_and_explicit_policy_decoder_are_frozen(monkeypatch):
    episode = build_episode({"layout": "open-kitchen"})
    configuration = PlayerConfiguration.model_validate(episode.player_config_message(2, "fixture"))
    monkeypatch.setenv("PLAYER_PROMPT", "  Preserve my\nprivate strategy.  ")
    monkeypatch.setenv("COWORLD_LLM_MODEL", "checkpoint:registered-policy-input")
    monkeypatch.setenv("COWORLD_LLM_TEMPERATURE", "0.7")
    native = registration(configuration)
    assert native.slot == 2 and native.prompt == "  Preserve my\nprivate strategy.  "
    assert native.profile.temperature == 0.7
    assert native.profile.model == "checkpoint:registered-policy-input"
    monkeypatch.setenv("COWORLD_LLM_MODEL", "other-model")
    assert native.profile.model == "checkpoint:registered-policy-input"
    monkeypatch.setenv("COWORLD_LLM_TEMPERATURE", "nan")
    with pytest.raises(ValidationError):
        registration(configuration)


def test_scripted_registration_does_not_silently_replace_an_unknown_policy(monkeypatch):
    episode = build_episode({"layout": "open-kitchen"})
    configuration = PlayerConfiguration.model_validate(episode.player_config_message(0, "fixture"))
    monkeypatch.delenv("PLAYER_PROMPT", raising=False)
    monkeypatch.setenv("PLAYER_SCRIPTED", "not-a-baseline")
    with pytest.raises(ValidationError):
        registration(configuration)
