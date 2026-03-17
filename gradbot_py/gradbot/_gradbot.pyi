"""Type stubs for the gradbot._gradbot native extension module."""

from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Lang:
    En: Lang
    Fr: Lang
    Es: Lang
    De: Lang
    Pt: Lang

    def code(self) -> str: ...
    @property
    def rewrite_rules(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class Gender:
    Masculine: Gender
    Feminine: Gender

    def __str__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class Country:
    Us: Country
    Gb: Country
    Fr: Country
    De: Country
    Mx: Country
    Es: Country
    Br: Country

    def code(self) -> str: ...
    def __str__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

class AudioFormat:
    OggOpus: AudioFormat
    Pcm: AudioFormat
    Ulaw: AudioFormat

    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class FlagshipVoice:
    @property
    def name(self) -> str: ...
    @property
    def voice_id(self) -> str: ...
    @property
    def language(self) -> Lang: ...
    @property
    def country(self) -> Country: ...
    @property
    def gender(self) -> Gender: ...
    @property
    def description(self) -> str: ...

class ToolDef:
    name: str
    description: str
    parameters_json: str

    def __init__(self, name: str, description: str, parameters_json: str) -> None: ...

class SessionConfig:
    voice_id: str | None
    instructions: str | None
    language: Lang
    assistant_speaks_first: bool
    silence_timeout_s: float
    tools: list[ToolDef]
    flush_duration_s: float
    padding_bonus: float
    rewrite_rules: str | None
    stt_extra_config: str | None
    tts_extra_config: str | None
    llm_extra_config: str | None

    def __init__(
        self,
        voice_id: str | None = None,
        instructions: str | None = None,
        language: Lang = Lang.En,
        assistant_speaks_first: bool = True,
        silence_timeout_s: float = 5.0,
        tools: list[ToolDef] = ...,
        flush_duration_s: float = 0.5,
        padding_bonus: float = 0.0,
        rewrite_rules: str | None = None,
        stt_extra_config: str | None = None,
        tts_extra_config: str | None = None,
        llm_extra_config: str | None = None,
    ) -> None: ...

class ToolCallInfo:
    @property
    def call_id(self) -> str: ...
    @property
    def tool_name(self) -> str: ...
    @property
    def args_json(self) -> str: ...

class Event:
    @property
    def event_type(self) -> str: ...
    @property
    def data(self) -> Any | None: ...

class MsgOut:
    @property
    def msg_type(self) -> str: ...
    @property
    def data(self) -> Any | None: ...
    @property
    def text(self) -> str | None: ...
    @property
    def start_s(self) -> float | None: ...
    @property
    def stop_s(self) -> float | None: ...
    @property
    def turn_idx(self) -> int | None: ...
    @property
    def time_s(self) -> float | None: ...
    @property
    def event(self) -> Event | None: ...
    @property
    def tool_call(self) -> ToolCallInfo | None: ...
    @property
    def tool_call_handle(self) -> ToolCallHandlePy | None: ...
    @property
    def interrupted(self) -> bool: ...

# ---------------------------------------------------------------------------
# Async handles
# ---------------------------------------------------------------------------

class ToolCallHandlePy:
    async def send(self, result_json: str) -> None: ...
    async def send_error(self, error_message: str) -> None: ...

class SessionInputHandle:
    async def send_audio(self, data: bytes) -> None: ...
    async def send_config(self, config: SessionConfig) -> None: ...
    async def close(self) -> None: ...

class SessionOutputHandle:
    async def receive(self) -> MsgOut | None: ...

class GradbotClients:
    async def start_session(
        self,
        initial_config: SessionConfig | None = None,
        input_format: AudioFormat = AudioFormat.Pcm,
        output_format: AudioFormat = AudioFormat.OggOpus,
    ) -> tuple[SessionInputHandle, SessionOutputHandle]: ...

# ---------------------------------------------------------------------------
# Module functions
# ---------------------------------------------------------------------------

def init_logging() -> None: ...
def flagship_voices() -> list[FlagshipVoice]: ...
def flagship_voice(name: str) -> FlagshipVoice: ...

async def create_clients(
    gradium_api_key: str | None = None,
    gradium_base_url: str | None = None,
    llm_base_url: str | None = None,
    llm_model_name: str | None = None,
    llm_api_key: str | None = None,
    max_completion_tokens: int | None = None,
) -> GradbotClients: ...

async def run(
    gradium_api_key: str | None = None,
    gradium_base_url: str | None = None,
    llm_base_url: str | None = None,
    llm_model_name: str | None = None,
    llm_api_key: str | None = None,
    max_completion_tokens: int | None = None,
    session_config: SessionConfig | None = None,
    input_format: AudioFormat = AudioFormat.Pcm,
    output_format: AudioFormat = AudioFormat.OggOpus,
    gradbot_url: str | None = None,
    gradbot_api_key: str | None = None,
) -> tuple[SessionInputHandle, SessionOutputHandle]: ...
