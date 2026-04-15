"""Pydantic-based config for gradbot demos.

Config is loaded from:
1. Environment variables (prefixed: LLM_, GRADIUM_, GRADBOT_, etc.)
2. A config.yaml next to the demo's main.py (overrides shared demos/config.yaml)

Example config.yaml:

    llm:
      model: "mistralai/mistral-small-3.1-24b-instruct"
      base_url: "https://openrouter.ai/api/v1"
      api_key: "sk-or-..."
      extra_config:
        reasoning:
          effort: "none"

    gradium:
      api_key: "your-gradium-key"
      base_url: "https://api.gradium.ai/api"

    tts:
      padding_bonus: 1.5
      rewrite_rules: "en"
      extra_config:
        some_key: some_value

    stt:
      flush_duration_s: 0.8
      extra_config:
        some_key: some_value

    session:
      silence_timeout_s: 5.0
      assistant_speaks_first: true
"""

import functools
import json
import os
import pathlib
from typing import Any

import pydantic
import pydantic_settings
import yaml


class LLMConfig(pydantic.BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: pydantic.SecretStr | None = None
    extra_config: dict[str, Any] | None = None


class GradiumConfig(pydantic.BaseModel):
    api_key: pydantic.SecretStr | None = None
    base_url: str | None = None


class GradbotServerConfig(pydantic.BaseModel):
    url: str | None = None
    api_key: pydantic.SecretStr | None = None


class TTSConfig(pydantic.BaseModel):
    padding_bonus: float | None = None
    rewrite_rules: str | None = None
    extra_config: dict[str, Any] | None = None


class STTConfig(pydantic.BaseModel):
    flush_duration_s: float | None = None
    extra_config: dict[str, Any] | None = None


class SessionSettings(pydantic.BaseModel):
    silence_timeout_s: float | None = None
    assistant_speaks_first: bool | None = None


class Config(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    llm: LLMConfig = LLMConfig()
    gradium: GradiumConfig = GradiumConfig()
    gradbot_server: GradbotServerConfig = GradbotServerConfig()
    tts: TTSConfig = TTSConfig()
    stt: STTConfig = STTConfig()
    session: SessionSettings = SessionSettings()

    # Flat env vars (mapped to nested fields after init)
    llm_model: str | None = pydantic.Field(None, alias="LLM_MODEL")
    llm_base_url: str | None = pydantic.Field(None, alias="LLM_BASE_URL")
    llm_api_key: pydantic.SecretStr | None = pydantic.Field(
        None, alias="LLM_API_KEY"
    )
    gradium_api_key: pydantic.SecretStr | None = pydantic.Field(
        None, alias="GRADIUM_API_KEY"
    )
    gradium_base_url: str | None = pydantic.Field(
        None, alias="GRADIUM_BASE_URL"
    )
    gradbot_url: str | None = pydantic.Field(None, alias="GRADBOT_URL")
    gradbot_api_key: pydantic.SecretStr | None = pydantic.Field(
        None, alias="GRADBOT_API_KEY"
    )

    # Demo runtime settings (loaded from env vars directly)
    use_pcm: bool = False
    debug: bool = False
    flush_for_s: float = 0.5

    @property
    def audio_format(self):
        from ._gradbot import AudioFormat

        return AudioFormat.Pcm if self.use_pcm else AudioFormat.OggOpus

    def model_post_init(self, __context: Any) -> None:
        """Map flat env vars into nested config."""
        if self.llm_model and not self.llm.model:
            self.llm.model = self.llm_model
        if self.llm_base_url and not self.llm.base_url:
            self.llm.base_url = self.llm_base_url
        if self.llm_api_key and not self.llm.api_key:
            self.llm.api_key = self.llm_api_key
        if self.gradium_api_key and not self.gradium.api_key:
            self.gradium.api_key = self.gradium_api_key
        if self.gradium_base_url and not self.gradium.base_url:
            self.gradium.base_url = self.gradium_base_url
        if not self.gradium.base_url:
            self.gradium.base_url = "https://gradium.ai/api/"
        if self.gradbot_url and not self.gradbot_server.url:
            self.gradbot_server.url = self.gradbot_url
        if self.gradbot_api_key and not self.gradbot_server.api_key:
            self.gradbot_server.api_key = self.gradbot_api_key

    @property
    def client_kwargs(self) -> dict[str, Any]:
        """Kwargs for gradbot.run() or create_clients()."""
        get_secret = lambda v: v.get_secret_value() if v else None  # noqa: F401
        mapping = {
            "llm_model_name": self.llm.model,
            "llm_base_url": self.llm.base_url,
            "llm_api_key": get_secret(self.llm.api_key),
            "gradium_api_key": get_secret(self.gradium.api_key),
            "gradium_base_url": self.gradium.base_url,
            "gradbot_url": self.gradbot_server.url,
            "gradbot_api_key": get_secret(self.gradbot_server.api_key),
        }
        return {k: v for k, v in mapping.items() if v is not None}

    @property
    def session_kwargs(self) -> dict[str, Any]:
        """Kwargs for gradbot.SessionConfig()."""
        mapping: dict[str, Any] = {
            "padding_bonus": self.tts.padding_bonus,
            "rewrite_rules": self.tts.rewrite_rules,
            "flush_duration_s": self.stt.flush_duration_s or self.flush_for_s,
            "silence_timeout_s": (self.session.silence_timeout_s),
            "assistant_speaks_first": (self.session.assistant_speaks_first),
            "llm_extra_config": (
                json.dumps(self.llm.extra_config)
                if self.llm.extra_config
                else None
            ),
            "tts_extra_config": (
                json.dumps(self.tts.extra_config)
                if self.tts.extra_config
                else None
            ),
            "stt_extra_config": (
                json.dumps(self.stt.extra_config)
                if self.stt.extra_config
                else None
            ),
        }
        return {k: v for k, v in mapping.items() if v is not None}


@functools.cache
def load(path: str | pathlib.Path = ".") -> Config:
    """Load config from YAML + environment variables.

    If *path* is a .yaml/.yml file, load it directly.
    If *path* is a directory, look for config.yaml in it,
    falling back to the parent directory.
    Environment variables override everything.
    """
    path = pathlib.Path(path).resolve()

    if path.suffix in (".yaml", ".yml"):
        with open(path) as f:
            return Config(**(yaml.safe_load(f) or {}))

    local = path / "config.yaml"
    if local.exists():
        return load(local)

    parent = path.parent / "config.yaml"
    if parent.exists():
        return load(parent)

    return Config()


def from_env(env_name: str = "CONFIG_DIR") -> Config:
    """Load config from CONFIG_DIR env var (cached)."""
    path = os.environ.get(env_name, ".")
    return load(pathlib.Path(path))
