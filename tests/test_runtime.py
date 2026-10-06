from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import runtime, settings


def test_selected_profile_drives_non_sensitive_inference_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "CONFIG_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr(settings, "_env", dict)
    monkeypatch.delenv("REELS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("REELS_MODEL_CODEX", raising=False)
    monkeypatch.delenv("REELS_CODEX_REASONING", raising=False)

    value = runtime.save(
        {
            "enabled": True,
            "day": "sun",
            "time": "10:00",
            "profile": "codex-terra-medium",
        }
    )

    assert runtime.profile_env(value) == {
        "REELS_LLM_BACKEND": "codex",
        "REELS_MODEL_CODEX": "gpt-5.6-terra",
        "REELS_CODEX_REASONING": "medium",
    }
    assert settings.llm_backend() == "codex"
    assert settings.model("extract") == "gpt-5.6-terra"
    assert settings.codex_reasoning() == "medium"


def test_runtime_profile_rejects_unknown_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "CONFIG_PATH", tmp_path / "runtime.json")

    try:
        runtime.save(
            {
                "enabled": False,
                "day": "sun",
                "time": "10:00",
                "profile": "arbitrary-command",
            }
        )
    except ValueError as error:
        assert str(error) == "invalid profile or day"
    else:
        raise AssertionError("unknown profile should be rejected")


def test_runtime_profile_does_not_override_env_before_user_selection(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "CONFIG_PATH", tmp_path / "runtime.yaml")
    monkeypatch.setattr(runtime, "LEGACY_CONFIG_PATH", tmp_path / "runtime.json")
    monkeypatch.setattr(
        settings,
        "_env",
        lambda: {"REELS_LLM_BACKEND": "ollama", "REELS_MODEL_EXTRACT": "local"},
    )
    monkeypatch.delenv("REELS_LLM_BACKEND", raising=False)

    assert runtime.profile_env() == {}
    assert settings.llm_backend() == "ollama"
    assert settings.model("extract") == "local"


def test_runtime_yaml_presence_prevents_legacy_json_fallback(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime.yaml"
    legacy_path = tmp_path / "runtime.json"
    legacy_path.write_text('{"profile": "codex-terra-medium"}', encoding="utf-8")
    monkeypatch.setattr(runtime, "CONFIG_PATH", config_path)
    monkeypatch.setattr(runtime, "LEGACY_CONFIG_PATH", legacy_path)

    assert runtime.load()["profile"] == "codex-terra-medium"

    config_path.write_text("{}\n", encoding="utf-8")
    assert runtime.load()["profile"] == runtime.DEFAULT["profile"]


def test_invalid_selected_profile_catalogue_is_not_silently_ignored(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "runtime.yaml"
    catalogue_path = tmp_path / "llm-profiles.yaml"
    config_path.write_text("profile: codex-luna-low\n", encoding="utf-8")
    catalogue_path.write_text("version: 1\nprofiles: []\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "CONFIG_PATH", config_path)
    monkeypatch.setattr(runtime, "PROFILE_CATALOG_PATH", catalogue_path)

    with pytest.raises(ValueError, match="invalid project LLM profile catalogue"):
        settings.llm_backend()


@pytest.mark.parametrize(
    "profile",
    [
        {"label": "No local model", "backend": "ollama", "intended_for": "Test"},
        {
            "label": "Unsupported Hermes model",
            "backend": "hermes",
            "model": "gpt-5.6-luna",
            "reasoning": "low",
            "intended_for": "Test",
        },
        {
            "label": "Incomplete Codex profile",
            "backend": "codex",
            "model": "gpt-5.6-luna",
            "intended_for": "Test",
        },
        {
            "label": "Incomplete API profile",
            "backend": "api",
            "intended_for": "Test",
        },
    ],
)
def test_profile_catalogue_rejects_backend_incoherence(profile):
    with pytest.raises(ValidationError):
        runtime.ProfileCatalog.model_validate(
            {"version": 1, "profiles": {"bad": profile}}
        )


def test_local_profile_catalogue_cannot_override_project_profile(tmp_path, monkeypatch):
    local_catalogue = tmp_path / "llm-profiles.local.yaml"
    local_catalogue.write_text(
        """version: 1
profiles:
  codex-luna-low:
    label: Local duplicate
    backend: codex
    model: gpt-5.6-luna
    reasoning: low
    intended_for: Test
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "LOCAL_PROFILE_PATH", local_catalogue)

    with pytest.raises(ValueError, match="duplicates project profile: codex-luna-low"):
        runtime.profiles()
