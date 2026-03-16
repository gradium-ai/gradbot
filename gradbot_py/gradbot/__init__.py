"""Python bindings for gradbot voice AI library."""

from __future__ import annotations

import json

from gradbot._gradbot import (  # noqa: F401
    Lang,
    Gender,
    Country,
    FlagshipVoice,
    ToolDef,
    ToolCallInfo,
    ToolCallHandlePy,
    SessionConfig,
    Event,
    MsgOut,
    SessionInputHandle,
    SessionOutputHandle,
    AudioFormat,
    GradbotClients,
    init_logging,
    flagship_voices,
    flagship_voice,
    create_clients,
    run,
)

__all__ = [
    "Lang", "Gender", "Country", "FlagshipVoice",
    "ToolDef", "ToolCallInfo", "ToolCallHandlePy",
    "SessionConfig", "Event", "MsgOut",
    "SessionInputHandle", "SessionOutputHandle",
    "AudioFormat", "GradbotClients",
    "init_logging", "flagship_voices", "flagship_voice",
    "create_clients", "run",
    "voices_json", "voice_switching_tools", "resolve_voice_from_tool",
]


_EMPTY_PARAMS_JSON = json.dumps({"type": "object", "properties": {}, "required": []})

# Cached results — built lazily on first access.
_voices_json: list[dict] | None = None
_voice_tools: list[ToolDef] | None = None
_voice_by_lower_name: dict[str, FlagshipVoice] | None = None


def _ensure_voice_cache() -> None:
    global _voices_json, _voice_tools, _voice_by_lower_name
    if _voice_by_lower_name is not None:
        return
    voices = flagship_voices()
    _voices_json = [
        {
            "name": v.name,
            "voice_id": v.voice_id,
            "language": v.language.code(),
            "country": v.country.code(),
            "country_name": str(v.country),
            "gender": str(v.gender),
            "description": v.description,
        }
        for v in voices
    ]
    _voice_tools = [
        ToolDef(
            name=f"switch_to_{v.name.lower()}",
            description=f"Switch to {v.name}'s voice. {v.description}",
            parameters_json=_EMPTY_PARAMS_JSON,
        )
        for v in voices
    ]
    _voice_by_lower_name = {v.name.lower(): v for v in voices}


def voices_json() -> list[dict]:
    """Return all flagship voices as JSON-serializable dicts."""
    _ensure_voice_cache()
    return _voices_json  # type: ignore[return-value]


def voice_switching_tools() -> list[ToolDef]:
    """Return ``switch_to_{name}`` tool definitions for all flagship voices."""
    _ensure_voice_cache()
    return _voice_tools  # type: ignore[return-value]


def resolve_voice_from_tool(tool_name: str) -> FlagshipVoice | None:
    """Resolve a ``switch_to_*`` tool name to a :class:`FlagshipVoice`.

    Returns *None* if *tool_name* doesn't start with ``switch_to_`` or no
    matching voice is found.
    """
    if not tool_name.startswith("switch_to_"):
        return None
    _ensure_voice_cache()
    return _voice_by_lower_name.get(tool_name[len("switch_to_"):])  # type: ignore[union-attr]
