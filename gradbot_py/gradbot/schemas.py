"""Pydantic schemas for gradbot."""

import re
import typing
from typing import TypeVar

import pydantic

T = TypeVar("T")


class Voice(pydantic.BaseModel):
    name: str
    voice_id: str
    language: str
    country: str
    country_name: str
    gender: str
    description: str


class BaseMessage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(from_attributes=True)

    # Used for discriminated union (matches MsgOut.msg_type).
    # Excluded from serialization.
    msg_type: str = pydantic.Field(exclude=True)

    # Wire format type sent to clients.
    type: str


class AudioTiming(BaseMessage):
    msg_type: typing.Literal["audio"] = pydantic.Field(
        default="audio", exclude=True
    )
    type: typing.Literal["audio_timing"] = "audio_timing"
    start_s: float | None
    stop_s: float | None
    turn_idx: int | None
    interrupted: bool


class TextMessage(BaseMessage):
    text: str
    stop_s: float | None = None
    turn_idx: int | None = None


class UserText(TextMessage):
    msg_type: typing.Literal["stt_text"] = pydantic.Field(
        default="stt_text", exclude=True
    )
    type: typing.Literal["user_text"] = "user_text"


CONTROL_TOKENS = re.compile(
    r"<\|[^>]*(?:\|>|>)"
    r"|<\|[^a-zA-Z0-9\s]+"
    r"|<[a-z_]+\|>"
)
IGNORE_WORDS = {"thought", "response"}


def sanitize(value: T) -> T:
    """Recursively strip LLM control-token junk from strings."""
    if isinstance(value, str):
        return CONTROL_TOKENS.sub("", value).strip()
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    return value


class AgentText(TextMessage):
    msg_type: typing.Literal["tts_text"] = pydantic.Field(
        default="tts_text", exclude=True
    )
    type: typing.Literal["agent_text"] = "agent_text"

    @pydantic.field_validator("text")
    @classmethod
    def _sanitize_text(cls, v: str) -> str:
        cleaned = sanitize(v)
        if not cleaned or cleaned.lower() in IGNORE_WORDS:
            raise ValueError("empty after sanitization")
        return cleaned


class SessionEvent(BaseMessage):
    msg_type: typing.Literal["event"] = pydantic.Field(
        default="event", exclude=True
    )
    type: typing.Literal["event"] = "event"
    event: str


class Error(BaseMessage):
    msg_type: typing.Literal["error"] = pydantic.Field(
        default="error", exclude=True
    )
    type: typing.Literal["error"] = "error"
    message: str


ServerMessage = typing.Annotated[
    AudioTiming | UserText | AgentText | SessionEvent | Error,
    pydantic.Discriminator("msg_type"),
]
adapter = pydantic.TypeAdapter(ServerMessage)

_MSG_ATTRS = ("text", "start_s", "stop_s", "turn_idx", "interrupted")


def from_msg(msg: typing.Any) -> BaseMessage | None:
    """Build a ServerMessage from a MsgOut object.

    Returns None for unknown msg_types (e.g. tool_call)
    or if validation fails (e.g. junk-only TTS text).
    """
    data = {"msg_type": msg.msg_type} | {
        attr: v
        for attr in _MSG_ATTRS
        if (v := getattr(msg, attr, None)) is not None
    }
    if msg.event is not None:
        data["event"] = msg.event.event_type
    try:
        return adapter.validate_python(data)
    except pydantic.ValidationError:
        return None
