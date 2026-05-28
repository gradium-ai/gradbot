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


def test_voice_config_uses_long_silence_checkin():
    start_context = {
        "fresh_intake": False,
        "confirmation_status": "draft",
        "missing_fields": ["work_location"],
        "last_search_result_count": 0,
        "saved_listings_count": 0,
        "drafts_count": 0,
    }

    config = gradbot_session._make_config(
        language="en",
        config=SimpleNamespace(session_kwargs={}),
        start_context=start_context,
    )

    assert config.silence_timeout_s == gradbot_session.SILENCE_CHECK_TIMEOUT_S
    assert config.silence_timeout_s >= 10.0
    assert 'latest user message is "..."' in config.instructions
    assert "Do not call tools" in config.instructions
    assert "Do not repeat the previous assistant message" in config.instructions


def test_voice_config_requires_confirm_before_searching_complete_draft():
    start_context = {
        "fresh_intake": False,
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

    assert "call confirm_profile first, then run_apartment_search" in config.instructions
    search_tool = next(tool for tool in config.tools if tool.name == "run_apartment_search")
    assert "call confirm_profile first" in search_tool.description
    assert "search without confirming" in search_tool.description


def test_voice_config_rejects_out_of_scope_property_types():
    start_context = {
        "fresh_intake": False,
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

    assert "ONLY for Paris apartment rentals" in config.instructions
    assert "castle" in config.instructions
    assert "do not call tools and do not update the profile" in config.instructions
    extract_tool = next(tool for tool in config.tools if tool.name == "extract_requirements_from_transcript")
    assert "Do NOT call this for out-of-scope property types" in extract_tool.description
    assert "castles" in extract_tool.description


def test_voice_config_can_target_berlin_market():
    start_context = {
        "city": "berlin",
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

    assert '"What kind of apartment are you looking for in Berlin?"' in config.instructions
    assert "ONLY for Berlin apartment rentals" in config.instructions
    extract_tool = next(tool for tool in config.tools if tool.name == "extract_requirements_from_transcript")
    assert "supports Berlin apartment rentals" in extract_tool.description


def test_unsupported_property_type_guard():
    assert gradbot_session._unsupported_property_type("I'm looking for a castle") == "castle"
    assert gradbot_session._unsupported_property_type("I want a château in Paris") == "chateau"
    assert gradbot_session._unsupported_property_type("I'm looking for houses") == "houses"
    assert gradbot_session._unsupported_property_type("I want a studio") is None
    assert gradbot_session._unsupported_property_type("I need a flat with castle vibes") is None
    assert gradbot_session._unsupported_property_type("Not a house, I need an apartment") is None


def test_unsupported_property_tool_result_pushes_back_without_update():
    result = gradbot_session._unsupported_property_tool_result("castle")

    assert result["ok"] is False
    assert result["error"] == "unsupported_property_type"
    assert "Do not say the profile was updated" in result["voice_instruction"]
    assert "only supports Paris apartment" in result["voice_instruction"]


def test_unsupported_property_tool_result_uses_selected_city():
    result = gradbot_session._unsupported_property_tool_result("castle", city="berlin")

    assert "only supports Berlin apartment" in result["voice_instruction"]


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
    assert "Do not call update_profile_draft" in result["voice_instruction"]
    assert "Do not repeat" in result["voice_instruction"]


def test_action_tool_result_guides_completion_acknowledgement():
    result = gradbot_session._action_tool_result(
        {"ok": True, "listing_id": "listing-1", "draft": {"body": "Hello"}},
        "drafted",
    )

    assert result["ok"] is True
    assert result["message"] == "Action completed: drafted the viewing message."
    assert "combine them into one concise acknowledgement" in result["voice_instruction"]
    assert "draft" not in result


def test_action_tool_result_guides_failed_action_without_inventing():
    result = gradbot_session._action_tool_result(
        {"ok": False, "error": "listing_not_found"},
        "saved",
    )

    assert "did not complete" in result["voice_instruction"]
    assert "Do not invent a listing" in result["voice_instruction"]
