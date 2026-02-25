"""
Shared YAML config loader for gradbot demos.

Place a config.yaml next to your demo's main.py to override SessionConfig defaults.
Values from the YAML are merged into SessionConfig kwargs.

Example config.yaml:

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
from pathlib import Path
from typing import Any

import yaml


def load_config(demo_dir: str | Path) -> dict[str, Any]:
    """Load config.yaml from the given demo directory. Returns empty dict if not found."""
    config_path = Path(demo_dir) / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    print(f"Loaded config from {config_path}")
    return config


def session_config_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """
    Convert YAML config into kwargs suitable for pygradbot.SessionConfig().

    Returns a dict that can be unpacked into SessionConfig:
        overrides = session_config_overrides(config)
        pygradbot.SessionConfig(voice_id=..., instructions=..., **overrides)
    """
    if not config:
        return {}

    overrides: dict[str, Any] = {}

    tts = config.get("tts", {})
    stt = config.get("stt", {})
    session = config.get("session", {})

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
        config = pygradbot.SessionConfig(
            voice_id=..., instructions=..., language=..., tools=...,
            **sc,
        )
    """
    result = dict(base_kwargs)
    result.update(overrides)
    return result
