"""Tests for voice-related functions: flagship_voices, flagship_voice,
voices_json, voice_switching_tools, resolve_voice_from_tool."""

import pytest
import gradbot


class TestFlagshipVoices:
    def test_returns_non_empty_list(self):
        voices = gradbot.flagship_voices()
        assert isinstance(voices, list)
        assert len(voices) > 0

    def test_voice_has_expected_attributes(self):
        voice = gradbot.flagship_voices()[0]
        assert isinstance(voice.name, str) and voice.name
        assert isinstance(voice.voice_id, str) and voice.voice_id
        assert isinstance(voice.language, gradbot.Lang)
        assert isinstance(voice.country, gradbot.Country)
        assert isinstance(voice.gender, gradbot.Gender)
        assert isinstance(voice.description, str)


class TestFlagshipVoice:
    def test_lookup_by_name(self):
        all_voices = gradbot.flagship_voices()
        first = all_voices[0]
        looked_up = gradbot.flagship_voice(first.name)
        assert looked_up.name == first.name
        assert looked_up.voice_id == first.voice_id

    def test_case_insensitive(self):
        all_voices = gradbot.flagship_voices()
        first = all_voices[0]
        looked_up = gradbot.flagship_voice(first.name.lower())
        assert looked_up.name == first.name

    def test_unknown_raises(self):
        with pytest.raises(RuntimeError):
            gradbot.flagship_voice("nonexistent_voice_xyz")


class TestVoicesJson:
    EXPECTED_KEYS = {"name", "voice_id", "language", "country", "country_name", "gender", "description"}

    def test_returns_list_of_dicts(self):
        result = gradbot.voices_json()
        assert isinstance(result, list)
        assert len(result) > 0
        assert isinstance(result[0], dict)

    def test_dicts_have_expected_keys(self):
        for entry in gradbot.voices_json():
            assert set(entry.keys()) == self.EXPECTED_KEYS

    def test_values_match_voice_data(self):
        voices = gradbot.flagship_voices()
        json_list = gradbot.voices_json()
        assert len(json_list) == len(voices)
        for voice, entry in zip(voices, json_list):
            assert entry["name"] == voice.name
            assert entry["voice_id"] == voice.voice_id
            assert entry["language"] == voice.language.code()
            assert entry["country"] == voice.country.code()
            assert entry["country_name"] == str(voice.country)
            assert entry["gender"] == str(voice.gender)


class TestVoiceSwitchingTools:
    def test_returns_one_per_voice(self):
        tools = gradbot.voice_switching_tools()
        voices = gradbot.flagship_voices()
        assert len(tools) == len(voices)

    def test_tool_names_follow_pattern(self):
        for tool in gradbot.voice_switching_tools():
            assert tool.name.startswith("switch_to_")
            # Name after prefix should be lowercase
            suffix = tool.name[len("switch_to_"):]
            assert suffix == suffix.lower()

    def test_tools_are_tooldef_instances(self):
        for tool in gradbot.voice_switching_tools():
            assert isinstance(tool, gradbot.ToolDef)
            assert tool.description
            assert tool.parameters_json


class TestResolveVoiceFromTool:
    def test_resolves_valid_names(self):
        voices = gradbot.flagship_voices()
        for voice in voices:
            tool_name = f"switch_to_{voice.name.lower()}"
            resolved = gradbot.resolve_voice_from_tool(tool_name)
            assert resolved is not None
            assert resolved.name == voice.name

    def test_returns_none_for_non_matching_prefix(self):
        assert gradbot.resolve_voice_from_tool("change_to_emma") is None

    def test_returns_none_for_nonexistent_voice(self):
        assert gradbot.resolve_voice_from_tool("switch_to_nonexistent_xyz") is None
