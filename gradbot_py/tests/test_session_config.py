"""Tests for SessionConfig construction."""

import gradbot


class TestSessionConfig:
    def test_minimal_construction(self):
        config = gradbot.SessionConfig()
        assert config.voice_id is None
        assert config.instructions is None
        assert config.language == gradbot.Lang.En
        assert config.assistant_speaks_first is True
        assert config.tools == []

    def test_full_construction(self):
        tool = gradbot.ToolDef(
            name="test_tool",
            description="A test tool",
            parameters_json='{"type": "object", "properties": {}, "required": []}',
        )
        config = gradbot.SessionConfig(
            voice_id="abc123",
            instructions="Be helpful",
            language=gradbot.Lang.Fr,
            assistant_speaks_first=False,
            silence_timeout_s=10.0,
            tools=[tool],
            flush_duration_s=1.0,
            padding_bonus=2.0,
            rewrite_rules="fr",
        )
        assert config.voice_id == "abc123"
        assert config.instructions == "Be helpful"
        assert config.language == gradbot.Lang.Fr
        assert config.assistant_speaks_first is False
        assert config.silence_timeout_s == 10.0
        assert config.flush_duration_s == 1.0
        assert config.padding_bonus == 2.0
        assert config.rewrite_rules == "fr"

    def test_tools_list_preserved(self):
        tools = [
            gradbot.ToolDef(name="a", description="tool a", parameters_json="{}"),
            gradbot.ToolDef(name="b", description="tool b", parameters_json="{}"),
        ]
        config = gradbot.SessionConfig(tools=tools)
        assert len(config.tools) == 2
        assert config.tools[0].name == "a"
        assert config.tools[1].name == "b"
