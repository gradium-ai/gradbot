"""Tests for gradbot.schemas."""

import gradbot


def test_sanitize_str():
    assert gradbot.schemas.sanitize("<|channel|>hello") == "hello"


def test_sanitize_strips_whitespace():
    assert gradbot.schemas.sanitize("  <|junk|>  text  ") == "text"


def test_sanitize_dict():
    result = gradbot.schemas.sanitize(
        {"key": "<|junk|>value", "nested": {"a": "clean"}}
    )
    assert result == {"key": "value", "nested": {"a": "clean"}}


def test_sanitize_list():
    result = gradbot.schemas.sanitize(
        ["<|x|>hello", "clean", 42]
    )
    assert result == ["hello", "clean", 42]


def test_sanitize_passthrough():
    assert gradbot.schemas.sanitize(42) == 42
    assert gradbot.schemas.sanitize(None) is None


class _Event:
    def __init__(self, event_type, data=None):
        self.event_type = event_type
        self.data = data


class _EventMsg:
    msg_type = "event"

    def __init__(self, event_type, turn_idx=None, data=None):
        self.event = _Event(event_type, data)
        self.turn_idx = turn_idx


def test_event_carries_turn():
    schema = gradbot.schemas.from_msg(_EventMsg("first_word", turn_idx=3))
    assert schema.model_dump() == {
        "type": "event",
        "event": "first_word",
        "turn_idx": 3,
    }


def test_event_data_is_forwarded():
    schema = gradbot.schemas.from_msg(
        _EventMsg("push_to_llm", turn_idx=1, data={"user_text": "hi"})
    )
    assert schema.model_dump()["data"] == {"user_text": "hi"}
