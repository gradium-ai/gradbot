"""Tests for voice functions."""

import pytest
import gradbot


def test_flagship_voices_non_empty():
    voices = gradbot.flagship_voices()
    assert len(voices) > 0


def test_voice_attributes():
    voice = gradbot.flagship_voices()[0]
    assert isinstance(voice.name, str) and voice.name
    assert isinstance(voice.voice_id, str) and voice.voice_id
    assert isinstance(voice.language, gradbot.Lang)
    assert isinstance(voice.country, gradbot.Country)
    assert isinstance(voice.gender, gradbot.Gender)


def test_flagship_voice_lookup():
    first = gradbot.flagship_voices()[0]
    looked_up = gradbot.flagship_voice(first.name)
    assert looked_up.voice_id == first.voice_id


def test_flagship_voice_case_insensitive():
    first = gradbot.flagship_voices()[0]
    looked_up = gradbot.flagship_voice(first.name.lower())
    assert looked_up.name == first.name


def test_flagship_voice_unknown_raises():
    with pytest.raises(RuntimeError):
        gradbot.flagship_voice("nonexistent_xyz")
