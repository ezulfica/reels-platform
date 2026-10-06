"""Versioned LLM profiles and user-owned runtime selection."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_CATALOG_PATH = PROJECT_ROOT / "config" / "llm-profiles.yaml"
CONFIG_DIR = Path.home() / ".config" / "reels-platform"
CONFIG_PATH = CONFIG_DIR / "runtime.yaml"
LEGACY_CONFIG_PATH = CONFIG_DIR / "runtime.json"
LOCAL_PROFILE_PATH = CONFIG_DIR / "llm-profiles.local.yaml"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
DAYS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


class ProfileBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    intended_for: str = Field(min_length=1)


class CodexProfile(ProfileBase):
    backend: Literal["codex"]
    model: str = Field(min_length=1)
    reasoning: Literal["low", "medium", "high"]


class HermesProfile(ProfileBase):
    backend: Literal["hermes"]
    reasoning: Literal["low", "medium", "high"]


class ApiProfile(ProfileBase):
    backend: Literal["api"]
    model: str = Field(min_length=1)


class OllamaProfile(ProfileBase):
    backend: Literal["ollama"]
    model: str = Field(min_length=1)


Profile: TypeAlias = Annotated[
    CodexProfile | HermesProfile | ApiProfile | OllamaProfile,
    Field(discriminator="backend"),
]


class ProfileCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    profiles: dict[str, Profile] = Field(min_length=1)


DEFAULT = {"enabled": True, "day": "sun", "time": "10:00", "profile": "codex-luna-low"}


def _read_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML in {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a YAML mapping")
    return value


def profiles() -> dict[str, Profile]:
    """Merge the versioned catalogue with optional local, non-secret profiles."""
    try:
        catalogue = ProfileCatalog.model_validate(_read_yaml(PROFILE_CATALOG_PATH))
    except ValidationError as error:
        raise ValueError(f"invalid project LLM profile catalogue: {error}") from error
    local = _read_yaml(LOCAL_PROFILE_PATH)
    if not local:
        return catalogue.profiles
    try:
        local_catalogue = ProfileCatalog.model_validate(local)
    except ValidationError as error:
        raise ValueError(f"invalid local LLM profile catalogue: {error}") from error
    overlap = set(catalogue.profiles) & set(local_catalogue.profiles)
    if overlap:
        raise ValueError(
            f"local LLM profile duplicates project profile: {sorted(overlap)[0]}"
        )
    return {**catalogue.profiles, **local_catalogue.profiles}


def load() -> dict:
    if CONFIG_PATH.exists():
        value = _read_yaml(CONFIG_PATH)
    else:
        try:
            value = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
    if not isinstance(value, dict):
        value = {}
    selected = value.get("profile", DEFAULT["profile"])
    return {
        **DEFAULT,
        **{key: value[key] for key in DEFAULT if key in value},
        "profile": selected,
    }


def profile_env(value: dict | None = None) -> dict[str, str]:
    if value is None and not CONFIG_PATH.exists() and not LEGACY_CONFIG_PATH.exists():
        return {}
    selected = value or load()
    profile_id = selected.get("profile", DEFAULT["profile"])
    profile = profiles().get(profile_id)
    if profile is None:
        raise ValueError(f"unknown LLM profile: {profile_id}")
    if isinstance(profile, CodexProfile):
        return {
            "REELS_LLM_BACKEND": "codex",
            "REELS_MODEL_CODEX": profile.model,
            "REELS_CODEX_REASONING": profile.reasoning,
        }
    if isinstance(profile, HermesProfile):
        return {
            "REELS_LLM_BACKEND": "hermes",
            "REELS_HERMES_REASONING": profile.reasoning,
        }
    if isinstance(profile, ApiProfile):
        return {"REELS_LLM_BACKEND": "api", "REELS_MODEL_API": profile.model}
    return {
        "REELS_LLM_BACKEND": "ollama",
        "REELS_MODEL_EXTRACT": profile.model,
    }


def save(value: dict) -> dict:
    available = profiles()
    profile = value.get("profile")
    if profile not in available or value.get("day") not in DAYS:
        raise ValueError("invalid profile or day")
    time = str(value.get("time", ""))
    if not re.fullmatch(r"\d{2}:\d{2}", time):
        raise ValueError("time must use HH:MM")
    hour, minute = map(int, time.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("invalid time")
    current = {
        "enabled": bool(value.get("enabled")),
        "day": value["day"],
        "time": time,
        "profile": profile,
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)
    return current


def apply(value: dict) -> None:
    service_dir = SYSTEMD_DIR / "reels-weekly.service.d"
    timer_dir = SYSTEMD_DIR / "reels-weekly.timer.d"
    service_dir.mkdir(parents=True, exist_ok=True)
    timer_dir.mkdir(parents=True, exist_ok=True)
    env = "\n".join(
        f"Environment={key}={item}" for key, item in profile_env(value).items()
    )
    (service_dir / "10-runtime.conf").write_text("[Service]\n" + env + "\n")
    (timer_dir / "10-runtime.conf").write_text(
        f"[Timer]\nOnCalendar=\nOnCalendar={DAYS[value['day']]} *-*-* {value['time']}:00\nPersistent=true\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    command = [
        "systemctl",
        "--user",
        "enable" if value["enabled"] else "disable",
        "--now",
        "reels-weekly.timer",
    ]
    subprocess.run(command, check=True)
