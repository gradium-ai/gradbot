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
