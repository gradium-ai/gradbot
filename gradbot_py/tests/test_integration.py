"""Integration tests covering cross-module workflows and untested modules.

Gaps addressed:
- demo_config.py: zero prior coverage despite being used by every demo
- setup_demo_routes: all options enabled simultaneously
- Voice pipeline: end-to-end voices_json -> voice_switching_tools -> resolve_voice_from_tool
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# demo_config.py lives in demos/, not in an installed package.
_DEMOS_DIR = Path(__file__).resolve().parents[2] / "demos"
sys.path.insert(0, str(_DEMOS_DIR))

from demo_config import client_config, load_config, merge_overrides, session_config_overrides

import gradbot
from gradbot.fastapi import setup_demo_routes


# ---------------------------------------------------------------------------
# demo_config: load_config YAML merge
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_shared_only(self, tmp_path: Path):
        demo = tmp_path / "my_demo"
        demo.mkdir()
        (tmp_path / "config.yaml").write_text("llm:\n  model: shared-model\n")

        cfg = load_config(demo)
        assert cfg["llm"]["model"] == "shared-model"

    def test_local_only(self, tmp_path: Path):
        demo = tmp_path / "my_demo"
        demo.mkdir()
        (demo / "config.yaml").write_text("tts:\n  padding_bonus: 2.0\n")

        cfg = load_config(demo)
        assert cfg["tts"]["padding_bonus"] == 2.0

    def test_local_overrides_shared_within_section(self, tmp_path: Path):
        demo = tmp_path / "my_demo"
        demo.mkdir()
        (tmp_path / "config.yaml").write_text(
            "llm:\n  model: shared\n  base_url: http://shared\n"
        )
        (demo / "config.yaml").write_text("llm:\n  model: local\n")

        cfg = load_config(demo)
        # Local overrides model but shared base_url is preserved (shallow merge)
        assert cfg["llm"]["model"] == "local"
        assert cfg["llm"]["base_url"] == "http://shared"

    def test_no_config_files(self, tmp_path: Path):
        demo = tmp_path / "my_demo"
        demo.mkdir()
        assert load_config(demo) == {}


# ---------------------------------------------------------------------------
# demo_config: client_config extraction
# ---------------------------------------------------------------------------

class TestClientConfig:
    def test_extracts_llm_and_gradium(self):
        cfg = {
            "llm": {"model": "m", "base_url": "http://llm", "api_key": "k1"},
            "gradium": {"api_key": "k2", "base_url": "http://g"},
        }
        result = client_config(cfg)
        assert result == {
            "llm_model_name": "m",
            "llm_base_url": "http://llm",
            "llm_api_key": "k1",
            "gradium_api_key": "k2",
            "gradium_base_url": "http://g",
        }

    def test_omits_missing_keys(self):
        result = client_config({"llm": {"model": "m"}})
        assert result == {"llm_model_name": "m"}


# ---------------------------------------------------------------------------
# demo_config: session_config_overrides + merge_overrides
# ---------------------------------------------------------------------------

class TestSessionConfigOverrides:
    def test_full_extraction(self):
        cfg = {
            "llm": {"extra_config": {"reasoning": {"effort": "none"}}},
            "tts": {"padding_bonus": 1.5, "rewrite_rules": "en"},
            "stt": {"flush_duration_s": 0.8},
            "session": {"silence_timeout_s": 5.0, "assistant_speaks_first": False},
        }
        ov = session_config_overrides(cfg)
        assert ov["padding_bonus"] == 1.5
        assert ov["rewrite_rules"] == "en"
        assert ov["flush_duration_s"] == 0.8
        assert ov["silence_timeout_s"] == 5.0
        assert ov["assistant_speaks_first"] is False
        assert '"effort"' in ov["llm_extra_config"]

    def test_empty_config(self):
        assert session_config_overrides({}) == {}

    def test_merge_overrides_priority(self):
        overrides = {"flush_duration_s": 0.8, "rewrite_rules": "fr"}
        merged = merge_overrides(overrides, flush_duration_s=1.2, rewrite_rules="en", padding_bonus=1.0)
        # YAML overrides win
        assert merged["flush_duration_s"] == 0.8
        assert merged["rewrite_rules"] == "fr"
        # base kwargs preserved when not overridden
        assert merged["padding_bonus"] == 1.0


# ---------------------------------------------------------------------------
# setup_demo_routes: all options enabled simultaneously
# ---------------------------------------------------------------------------

class TestSetupDemoRoutesAllOptions:
    def test_all_options_enabled(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("<h1>hi</h1>")
        (tmp_path / "app.js").write_text("console.log('hi')")

        app = FastAPI()
        setup_demo_routes(app, static_dir=tmp_path, use_pcm=True, voices=True)
        client = TestClient(app)

        # All three route families respond correctly
        assert client.get("/api/audio-config").json() == {"pcm": True}
        assert len(client.get("/api/voices").json()["voices"]) > 0
        assert "<h1>hi</h1>" in client.get("/").text
        assert "console.log" in client.get("/static/app.js").text


# ---------------------------------------------------------------------------
# Voice pipeline: end-to-end flow
# ---------------------------------------------------------------------------

class TestVoicePipelineEndToEnd:
    def test_voices_json_through_resolve(self):
        """voices_json -> voice_switching_tools -> resolve_voice_from_tool consistency."""
        json_list = gradbot.voices_json()
        tools = gradbot.voice_switching_tools()
        assert len(json_list) == len(tools)

        for entry, tool in zip(json_list, tools):
            expected_tool_name = f"switch_to_{entry['name'].lower()}"
            assert tool.name == expected_tool_name

            resolved = gradbot.resolve_voice_from_tool(tool.name)
            assert resolved is not None
            assert resolved.voice_id == entry["voice_id"]
            assert resolved.language.code() == entry["language"]
