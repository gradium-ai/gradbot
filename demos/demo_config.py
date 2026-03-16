"""
Shared YAML config loader for gradbot demos.

Place a config.yaml next to your demo's main.py to override SessionConfig defaults.
Values from the YAML are merged into SessionConfig kwargs.

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

import json
import os
from pathlib import Path
from typing import Any

import yaml


def load_config(demo_dir: str | Path) -> dict[str, Any]:
    """Load config.yaml from the demo directory, falling back to the shared demos/ directory.

    Per-demo values override the shared config.
    """
    demo_dir = Path(demo_dir)
    shared_path = demo_dir.parent / "config.yaml"
    local_path = demo_dir / "config.yaml"

    config: dict[str, Any] = {}
    if shared_path.exists():
        with open(shared_path) as f:
            config = yaml.safe_load(f) or {}
        print(f"Loaded shared config from {shared_path}")
    if local_path.exists():
        with open(local_path) as f:
            local = yaml.safe_load(f) or {}
        # Merge: local overrides shared (shallow per top-level key)
        for key, val in local.items():
            if isinstance(val, dict) and isinstance(config.get(key), dict):
                config[key] = {**config[key], **val}
            else:
                config[key] = val
        print(f"Loaded local config from {local_path}")
    return config


def client_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Extract client-level config (LLM and Gradium) from YAML config.

    Returns a dict with keys suitable for gradbot.run() or gradbot.create_clients():
        llm_model_name, llm_base_url, llm_api_key, gradium_api_key, gradium_base_url

    Only includes keys that are actually set in the YAML.

    Usage:
        _CLIENT_CONFIG = client_config(config)
        await gradbot.run(**_CLIENT_CONFIG, session_config=config, ...)
    """
    result: dict[str, Any] = {}

    llm = config.get("llm", {})
    gradium = config.get("gradium", {})

    if "model" in llm:
        result["llm_model_name"] = llm["model"]
    if "base_url" in llm:
        result["llm_base_url"] = llm["base_url"]
    if "api_key" in llm:
        result["llm_api_key"] = llm["api_key"]

    if "api_key" in gradium:
        result["gradium_api_key"] = gradium["api_key"]
    if "base_url" in gradium:
        result["gradium_base_url"] = gradium["base_url"]

    gradbot_server = config.get("gradbot_server", {})
    if "url" in gradbot_server:
        result["gradbot_url"] = gradbot_server["url"]
    elif env_url := os.environ.get("GRADBOT_URL"):
        result["gradbot_url"] = env_url
    if "api_key" in gradbot_server:
        result["gradbot_api_key"] = gradbot_server["api_key"]
    elif env_key := os.environ.get("GRADBOT_API_KEY"):
        result["gradbot_api_key"] = env_key

    return result


def session_config_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """
    Convert YAML config into kwargs suitable for gradbot.SessionConfig().

    Returns a dict that can be unpacked into SessionConfig:
        overrides = session_config_overrides(config)
        gradbot.SessionConfig(voice_id=..., instructions=..., **overrides)
    """
    if not config:
        return {}

    overrides: dict[str, Any] = {}

    llm = config.get("llm", {})
    tts = config.get("tts", {})
    stt = config.get("stt", {})
    session = config.get("session", {})

    # LLM settings
    if "extra_config" in llm:
        overrides["llm_extra_config"] = json.dumps(llm["extra_config"])

    # TTS settings
    if "padding_bonus" in tts:
        overrides["padding_bonus"] = float(tts["padding_bonus"])
    if "rewrite_rules" in tts:
        overrides["rewrite_rules"] = tts["rewrite_rules"]
    if "extra_config" in tts:
        overrides["tts_extra_config"] = json.dumps(tts["extra_config"])

    # STT settings
    if "flush_duration_s" in stt:
        overrides["flush_duration_s"] = float(stt["flush_duration_s"])
    if "extra_config" in stt:
        overrides["stt_extra_config"] = json.dumps(stt["extra_config"])

    # Session settings
    if "silence_timeout_s" in session:
        overrides["silence_timeout_s"] = float(session["silence_timeout_s"])
    if "assistant_speaks_first" in session:
        overrides["assistant_speaks_first"] = bool(session["assistant_speaks_first"])

    return overrides


def merge_overrides(overrides: dict[str, Any], **base_kwargs: Any) -> dict[str, Any]:
    """
    Merge YAML overrides with base SessionConfig kwargs. YAML values take priority.

    Usage:
        sc = merge_overrides(_OVERRIDES,
            flush_duration_s=FLUSH_FOR_S,
            rewrite_rules=voice.language.rewrite_rules,
        )
        config = gradbot.SessionConfig(
            voice_id=..., instructions=..., language=..., tools=...,
            **sc,
        )
    """
    result = dict(base_kwargs)
    result.update(overrides)
    return result
