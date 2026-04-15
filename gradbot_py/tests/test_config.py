"""Tests for gradbot.config."""

import pathlib
import tempfile

import gradbot


# ── SessionConfig ───────────────────────────────────────────


def test_session_config_minimal():
    cfg = gradbot.SessionConfig()
    assert cfg.voice_id is None
    assert cfg.instructions is None
    assert cfg.language == gradbot.Lang.En
    assert cfg.assistant_speaks_first is True
    assert cfg.tools == []


def test_session_config_full():
    tool = gradbot.ToolDef(
        name="test",
        description="A test tool",
        parameters_json='{"type":"object","properties":{},"required":[]}',
    )
    cfg = gradbot.SessionConfig(
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
    assert cfg.voice_id == "abc123"
    assert cfg.language == gradbot.Lang.Fr
    assert cfg.assistant_speaks_first is False
    assert cfg.flush_duration_s == 1.0
    assert cfg.padding_bonus == 2.0


def test_session_config_tools_preserved():
    tools = [
        gradbot.ToolDef(name="a", description="a", parameters_json="{}"),
        gradbot.ToolDef(name="b", description="b", parameters_json="{}"),
    ]
    cfg = gradbot.SessionConfig(tools=tools)
    assert len(cfg.tools) == 2
    assert cfg.tools[0].name == "a"


# ── Config loading ──────────────────────────────────────────


def test_load_from_yaml():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "config.yaml"
        p.write_text("llm:\n  model: test-model\n")
        cfg = gradbot.config.load(d)
        assert cfg.llm.model == "test-model"


def test_load_no_file():
    with tempfile.TemporaryDirectory() as d:
        cfg = gradbot.config.load(d)
        assert cfg.llm.model is None


def test_load_yaml_file_directly():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "settings.yaml"
        p.write_text("llm:\n  model: direct\n")
        cfg = gradbot.config.load(p)
        assert cfg.llm.model == "direct"


def test_client_kwargs():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "config.yaml"
        p.write_text(
            "llm:\n  model: m\n  base_url: http://llm\n"
            "gradium:\n  base_url: http://g\n"
        )
        cfg = gradbot.config.load(d)
        kw = cfg.client_kwargs
        assert kw["llm_model_name"] == "m"
        assert kw["llm_base_url"] == "http://llm"
        assert kw["gradium_base_url"] == "http://g"
        assert "llm_api_key" not in kw


def test_session_kwargs():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "config.yaml"
        p.write_text(
            "tts:\n  padding_bonus: 1.5\n"
            "stt:\n  flush_duration_s: 0.8\n"
        )
        cfg = gradbot.config.load(d)
        kw = cfg.session_kwargs
        assert kw["padding_bonus"] == 1.5
        assert kw["flush_duration_s"] == 0.8


def test_audio_format_default():
    with tempfile.TemporaryDirectory() as d:
        cfg = gradbot.config.load(d)
        assert cfg.audio_format == gradbot.AudioFormat.OggOpus


# ── Langs ───────────────────────────────────────────────────


def test_languages_dict():
    assert gradbot.LANGUAGES["en"] == gradbot.Lang.En
    assert gradbot.LANGUAGES["fr"] == gradbot.Lang.Fr


def test_language_names_dict():
    assert gradbot.LANGUAGE_NAMES["en"] == "English"
    assert gradbot.LANGUAGE_NAMES["fr"] == "French"
