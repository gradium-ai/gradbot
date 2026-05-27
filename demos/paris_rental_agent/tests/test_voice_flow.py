"""Voice-session prompt and tool gating tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.voice import gradbot_session


def _tool_names(tools):
    return {tool.name for tool in tools}


def test_fresh_voice_config_speaks_first_without_start_intake_tool():
    start_context = {
        "fresh_intake": True,
        "confirmation_status": "draft",
        "missing_fields": [],
        "last_search_result_count": 0,
        "saved_listings_count": 0,
        "drafts_count": 0,
    }

    config = gradbot_session._make_config(
        language="en",
        config=SimpleNamespace(session_kwargs={}),
        start_context=start_context,
        assistant_speaks_first=True,
    )

    assert config.assistant_speaks_first is True
    assert "Do not call any tools before that first question." in config.instructions
    assert '"What kind of apartment are you looking for in Paris?"' in config.instructions
    assert "start_profile_intake" not in _tool_names(config.tools)
    assert "extract_requirements_from_transcript" in _tool_names(config.tools)


def test_refreshed_voice_config_does_not_auto_speak():
    start_context = {
        "fresh_intake": True,
        "confirmation_status": "draft",
        "missing_fields": [],
        "last_search_result_count": 0,
        "saved_listings_count": 0,
        "drafts_count": 0,
    }

    config = gradbot_session._make_config(
        language="en",
        config=SimpleNamespace(session_kwargs={}),
        start_context=start_context,
    )

    assert config.assistant_speaks_first is False


def test_profile_tool_result_omits_summary_and_guides_voice_reply():
    result = gradbot_session._profile_tool_result(
        {
            "ok": True,
            "summary": "I've noted the user's apartment search.",
            "draft_profile": {"max_rent_including_charges_eur": 2000},
            "applied_fields": ["max_rent_including_charges_eur"],
            "missing_fields": [],
        }
    )

    assert "summary" not in result
    assert "draft_profile" not in result
    assert result["applied_fields"] == ["max_rent_including_charges_eur"]
    assert "Do not repeat" in result["voice_instruction"]
