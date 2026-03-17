"""Type stubs for the gradbot package."""

from gradbot._gradbot import (
    AudioFormat as AudioFormat,
    Country as Country,
    Event as Event,
    FlagshipVoice as FlagshipVoice,
    Gender as Gender,
    GradbotClients as GradbotClients,
    Lang as Lang,
    MsgOut as MsgOut,
    SessionConfig as SessionConfig,
    SessionInputHandle as SessionInputHandle,
    SessionOutputHandle as SessionOutputHandle,
    ToolCallHandlePy as ToolCallHandlePy,
    ToolCallInfo as ToolCallInfo,
    ToolDef as ToolDef,
    create_clients as create_clients,
    flagship_voice as flagship_voice,
    flagship_voices as flagship_voices,
    init_logging as init_logging,
    run as run,
)

def voices_json() -> list[dict[str, str]]: ...
def voice_switching_tools() -> list[ToolDef]: ...
def resolve_voice_from_tool(tool_name: str) -> FlagshipVoice | None: ...
