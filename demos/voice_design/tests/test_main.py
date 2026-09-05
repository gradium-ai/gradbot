"""Focused tests for the voice-design request and response boundary."""

import asyncio
import json
import pathlib
import struct
import sys

import httpx
import pytest
import gradbot

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

import main
import voice_generator


def test_harper_is_the_english_starter_voice():
    state = main.VoiceDesignState()
    config = main._make_config(state, speaks_first=True)

    assert main.DEFAULT_VOICE_ID == "4SZHfMpw-p46Ywgs"
    assert state.voice_id == main.DEFAULT_VOICE_ID
    assert state.language == "en"
    assert config.voice_id == main.DEFAULT_VOICE_ID
    assert "Conversation language: English (en)" in config.instructions
    assert config.tools == []
    startup = json.loads(config.llm_extra_config)
    assert startup["tool_choice"] == "none"
    assert startup["stop"] == ["?"]
    assert startup["max_tokens"] == 64
    assert startup["chat_template_kwargs"] == {"enable_thinking": False}


def test_native_phone_tool_formats_first_draft_and_sends_only_later_changes():
    preview_tool = next(tool for tool in main.TOOLS if tool.name == "preview_voice")
    schema = json.loads(preview_tool.parameters_json)

    assert schema["required"] == ["voice_description"]
    assert list(schema["properties"]) == ["voice_description"]
    description = schema["properties"]["voice_description"]["description"]
    assert "For the first draft" in description
    assert "For revisions" in description


def test_switch_language_tool_uses_supported_language_codes():
    language_tool = next(tool for tool in main.TOOLS if tool.name == "switch_language")
    schema = json.loads(language_tool.parameters_json)

    assert schema["required"] == ["language"]
    assert schema["properties"]["language"]["enum"] == [
        "en",
        "fr",
        "es",
        "de",
        "pt",
    ]
    assert "French voice" in language_tool.description
    assert "not a language switch" in language_tool.description


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Please speak French", "fr"),
        ("Can we switch the conversation to Spanish?", "es"),
        ("Continue our conversation in German", "de"),
        ("Answer me in Portuguese from now on", "pt"),
        ("Let's talk in English", "en"),
        ("Passe la conversation en anglais, s'il te plaît.", "en"),
        ("Continúa la conversación en francés.", "fr"),
        ("Bitte wechsle die Unterhaltung zu Spanisch.", "es"),
        ("Mude a conversa para alemão.", "de"),
    ],
)
def test_explicit_language_commands_are_detected(text, language):
    assert main._is_explicit_language_switch(text, language)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("I want a French voice", "fr"),
        ("Create a warm Spanish accent", "es"),
        ("Make her sound like a German woman", "de"),
        ("A Portuguese-accented male voice", "pt"),
        ("Give the character an English voice", "en"),
        ("", "fr"),
    ],
)
def test_voice_traits_are_not_language_switches(text, language):
    assert not main._is_explicit_language_switch(text, language)


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("Please use your original agent voice again.", "agent"),
        ("Now go back to the designed voice.", "designed"),
        ("Now go back to the design voice.", "designed"),
    ],
)
def test_explicit_conversation_voice_switches_are_detected(text, target):
    assert main._requested_voice_source(text) == target


def test_voice_edit_is_not_mistaken_for_conversation_voice_switch():
    assert main._requested_voice_source("Make the same voice female.") is None


def test_revision_prompt_appends_latest_changes_and_makes_them_authoritative():
    description = main._append_voice_changes(
        "A young French-accented male voice with bright energy",
        "Make the same voice older and female",
    )

    assert description.startswith("A young French-accented male voice")
    assert "override any conflicts" in description
    assert description.endswith("Make the same voice older and female")
    assert len(description) <= voice_generator.MAX_DESCRIPTION_CHARS


def test_revision_prompt_preserves_latest_change_at_the_character_limit():
    latest = "female, older, calmer, and more deliberate"
    description = main._append_voice_changes("x" * 490, latest)

    assert description.endswith(latest)
    assert len(description) <= voice_generator.MAX_DESCRIPTION_CHARS


def test_revision_prompt_does_not_append_a_repeated_full_description():
    current = "deep mythical male, confident, husky, slow paced"

    assert main._append_voice_changes(current, current) == current
    assert main._append_voice_changes(
        current, f"{current}, more playful"
    ) == f"{current}, more playful"


def test_acknowledgement_suggestion_changes_with_each_revision():
    first = main._system_prompt(main.VoiceDesignState(revision=0))
    second = main._system_prompt(main.VoiceDesignState(revision=1))

    assert main.ACKNOWLEDGEMENTS[0] in first
    assert main.ACKNOWLEDGEMENTS[1] in second
    assert main.ACKNOWLEDGEMENTS[0] not in second


def test_prompt_tracks_the_live_conversation_language():
    state = main.VoiceDesignState(language="fr")
    config = main._make_config(state)

    assert "Conversation language: French (fr)" in config.instructions
    assert "Speak French" in config.instructions
    assert "reset_asr exactly once" in config.instructions
    assert '"French voice"' in config.instructions
    assert "must use preview_voice" in config.instructions
    assert config.language == gradbot.LANGUAGES["fr"]
    assert config.rewrite_rules == gradbot.LANGUAGES["fr"].rewrite_rules


def test_voice_design_limits_are_enforced():
    voice_generator.VoiceDesigner.validate_request("warm and calm", "Hello there", "en")
    with pytest.raises(voice_generator.VoiceDesignError, match="500 characters"):
        voice_generator.VoiceDesigner.validate_request("x" * 501, "Hello", "en")
    with pytest.raises(voice_generator.VoiceDesignError, match="100 characters"):
        voice_generator.VoiceDesigner.validate_request("warm", "x" * 101, "en")


def test_wav_duration_uses_the_patched_audio_header():
    wav = (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
        + (b"\x00\x00" * 48000)
    )

    assert voice_generator.wav_duration_s(voice_generator.fix_wav_sizes(wav)) == 1.0
    assert voice_generator.wav_duration_s(b"not a wav") == 0.0


def test_candidate_generation_and_finalization_follow_the_kit(tmp_path):
    calls = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/voice-generator/generate"):
            assert json.loads(request.content) == {
                "prompt": "warm and calm",
                "language": "en",
                "n_samples": 1,
                "json_config": {"cfg_scale": 10.0},
            }
            return httpx.Response(
                201,
                json={
                    "embeddings": [
                        {"embedding_id": "vox_emb_candidate", "ready": False}
                    ]
                },
            )
        if request.url.path.endswith("/voice-generator/embeddings"):
            poll_count += 1
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {
                            "embedding_id": "vox_emb_candidate",
                            "ready": poll_count > 1,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/speech/tts"):
            assert json.loads(request.content) == {
                "text": "Hello there",
                "voice_id": "vox_emb_candidate",
                "model_name": voice_generator.TTS_MODEL_NAME,
                "output_format": "wav",
                "only_audio": True,
            }
            wav = (
                b"RIFF"
                + struct.pack("<I", 0xFFFFFFFF)
                + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
                + b"data"
                + struct.pack("<I", 0xFFFFFFFF)
                + b"\x00\x00\x01\x00"
            )
            return httpx.Response(
                200, content=wav, headers={"content-type": "audio/wav"}
            )
        if request.url.path.endswith("/voices/from-embedding"):
            return httpx.Response(201, json={"uid": "permanent_voice"})
        if request.method == "PUT" and request.url.path.endswith(
            "/voices/permanent_voice"
        ):
            assert json.loads(request.content) == {
                "name": "Calm voice",
                "description": "warm and calm",
            }
            return httpx.Response(200, json={"uid": "permanent_voice"})
        if request.method == "DELETE" and request.url.path.endswith(
            "/voices/permanent_voice"
        ):
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run_flow():
        designer = voice_generator.VoiceDesigner(
            base_url="https://api.gradium.ai/api",
            api_key="test-key",
            store_path=tmp_path / "selections.sqlite3",
            poll_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            candidate_id = await designer.generate_candidate(
                "warm and calm", "Hello there", "en"
            )
            preview_audio = await designer.render_preview(candidate_id, "Hello there")
            voice_id = await designer.keep_candidate(
                candidate_id,
                name="Calm voice",
                description="warm and calm",
            )
            repeated_voice_id = await designer.keep_candidate(
                candidate_id,
                name="Calm voice",
                description="warm and calm",
            )
            await designer.update_voice(
                voice_id,
                name="Calm voice",
                description="warm and calm",
            )
            await designer.delete_voice(voice_id)
            return candidate_id, preview_audio, voice_id, repeated_voice_id
        finally:
            await designer.aclose()

    candidate_id, preview_audio, voice_id, repeated_voice_id = asyncio.run(run_flow())
    assert candidate_id == "vox_emb_candidate"
    assert voice_id == repeated_voice_id == "permanent_voice"
    assert struct.unpack_from("<I", preview_audio, 4)[0] == len(preview_audio) - 8
    assert struct.unpack_from("<I", preview_audio, 40)[0] == 4
    assert calls.count(("POST", "/api/voices/from-embedding")) == 1
    assert ("PUT", "/api/voices/permanent_voice") in calls
    assert ("DELETE", "/api/voices/permanent_voice") in calls


def test_promoted_draft_becomes_the_streaming_conversation_voice():
    state = main.VoiceDesignState(
        voice_id="draft_voice",
        draft_voice_id="draft_voice",
        description="warm and calm",
    )

    config = main._make_config(state)

    assert config.voice_id == "draft_voice"
    assert "latest designed voice" in config.instructions


def test_finalized_session_can_still_create_a_revision():
    state = main.VoiceDesignState(
        voice_id="saved_voice",
        draft_voice_id="saved_voice",
        saved_voice_ids={"saved_voice"},
        finalized=True,
        description="warm and calm",
    )
    config = main._make_config(state)

    assert {tool.name for tool in config.tools} == {
        "preview_voice",
        "finalize_voice",
        "switch_conversation_voice",
        "switch_language",
    }
    assert config.silence_timeout_s == 0.0
    assert "saved voice may still be revised" in " ".join(
        config.instructions.lower().split()
    )


def test_tools_are_suspended_while_voice_design_is_in_progress():
    state = main.VoiceDesignState(design_in_progress=True)
    config = main._make_config(state)

    assert config.tools == []
    assert "holding_phrase" in config.instructions
    assert "say that phrase exactly once" in config.instructions
    assert "preview_voice" not in config.instructions
    extra_config = json.loads(config.llm_extra_config)
    assert extra_config["max_tokens"] == 1
    assert extra_config["tool_choice"] == "none"
    assert "[Empty response]" in extra_config["stop"]


def test_preview_tool_stays_suspended_until_new_user_activity():
    state = main.VoiceDesignState(
        draft_voice_id="draft_voice",
        description="warm and calm",
        preview_armed=False,
    )
    config = main._make_config(state)

    assert config.tools == []
    assert "feedback question is played directly" in config.instructions
    assert "do not repeat or paraphrase" in config.instructions
    assert "preview_voice" not in config.instructions
    assert "finalize_voice" not in config.instructions
    extra_config = json.loads(config.llm_extra_config)
    assert extra_config["tool_choice"] == "none"
    assert extra_config["max_tokens"] == 1
    assert "[Empty response]" in extra_config["stop"]


def test_language_reset_continuation_exposes_only_the_internal_asr_tool():
    state = main.VoiceDesignState(
        language="fr",
        language_switch_in_progress=True,
        preview_armed=False,
    )

    config = main._make_config(state)

    assert config.tools == []
    assert "played directly" in config.instructions
    assert "call reset_asr exactly" in config.instructions
    extra_config = json.loads(config.llm_extra_config)
    assert extra_config["temperature"] == 0
    assert extra_config["tool_choice"] == {
        "type": "function",
        "function": {"name": "reset_asr"},
    }
    assert extra_config["max_tokens"] == 32


def test_direct_confirmation_suspends_tools_and_spoken_continuations():
    state = main.VoiceDesignState(
        draft_voice_id="draft_voice",
        description="warm and calm",
        direct_response_in_progress=True,
        preview_armed=False,
    )

    config = main._make_config(state)

    assert config.tools == []
    assert "played directly" in config.instructions
    extra_config = json.loads(config.llm_extra_config)
    assert extra_config["tool_choice"] == "none"
    assert extra_config["max_tokens"] == 1


def test_real_user_activity_restores_the_full_workflow():
    state = main.VoiceDesignState(
        draft_voice_id="draft_voice",
        description="warm and calm",
        preview_armed=True,
    )
    config = main._make_config(state)

    assert {tool.name for tool in config.tools} == {
        "preview_voice",
        "finalize_voice",
        "switch_conversation_voice",
        "switch_language",
    }
    assert "When the user requests a voice or a change" in config.instructions


def test_ordinary_conversation_route_disables_tools_and_requires_a_direct_answer():
    state = main.VoiceDesignState(
        draft_voice_id="draft_voice",
        description="warm and calm",
        preview_armed=True,
        turn_route="conversation",
    )

    config = main._make_config(state)
    extra = json.loads(config.llm_extra_config)

    assert config.tools == []
    assert "Answer it directly" in config.instructions
    assert "mention a system, process, or unfinished work" in config.instructions
    assert extra["tool_choice"] == "none"


def test_french_revision_route_allows_one_localized_preview_call():
    state = main.VoiceDesignState(
        language="fr",
        draft_voice_id="draft_voice",
        description="warm and calm",
        revision=1,
        preview_armed=True,
        turn_route="revision",
    )

    config = main._make_config(state)
    extra = json.loads(config.llm_extra_config)

    assert [tool.name for tool in config.tools] == ["preview_voice"]
    assert "Je vais essayer cette direction." in config.instructions
    assert extra["tool_choice"] == {
        "type": "function", "function": {"name": "preview_voice"}
    }


def test_language_route_forces_switch_tool_before_end_of_turn():
    state = main.VoiceDesignState(
        draft_voice_id="draft_voice",
        description="warm and calm",
        preview_armed=True,
        turn_route="language",
        pending_language="fr",
    )

    config = main._make_config(state)
    extra = json.loads(config.llm_extra_config)

    assert [tool.name for tool in config.tools] == ["switch_language"]
    assert "language='fr'" in config.instructions
    assert extra["tool_choice"]["function"]["name"] == "switch_language"


def test_original_agent_voice_can_be_restored_without_losing_the_draft():
    state = main.VoiceDesignState(
        voice_id=main.DEFAULT_VOICE_ID,
        agent_voice_id=main.DEFAULT_VOICE_ID,
        draft_voice_id="draft_voice",
        description="warm and calm",
    )
    config = main._make_config(state)

    assert config.voice_id == main.DEFAULT_VOICE_ID
    assert state.draft_voice_id == "draft_voice"
    assert "original agent voice" in config.instructions


def test_frontend_reports_complete_user_text_at_end_of_turn():
    html = (main.APP_DIR / "static" / "index.html").read_text()

    assert "currentUserTranscript" in html
    assert "user_text: currentUserTranscript" in html
    assert "voice_feedback_audio" in html
    assert "playVoiceFeedback" in html


def test_revision_appends_the_complete_latest_request():
    current = (
        "Feminine, Middle-aged. Scottish accent. Calm, gentle. "
        "A medium, velvety voice, slow."
    )

    revised = main._resolve_voice_description(
        current,
        "older",
        "Make it older, please.",
        "en",
    )

    assert revised.startswith(current)
    assert revised.endswith("Make it older, please.")
    assert "override any conflicts" in revised


@pytest.mark.parametrize(
    "text",
    [
        "Make it older, please.",
        "Same voice, but female.",
        "A little deeper.",
        "Give her a Scottish accent.",
        "Yes, it is, but I want the voice to be a bit more calming.",
        "I would like it to sound warmer and more mature.",
    ],
)
def test_explicit_revisions_are_safe_to_prefetch(text):
    import caption_edit

    current = "Masculine, Middle-aged. A medium, velvety voice, slow."
    assert caption_edit.is_explicit_edit_request(current, text, "en")


@pytest.mark.parametrize("text", [
    "Yes, it is. Now make it a voice which is more knowledgeable and someone like the voice of someone who's lived and seen more life.",
    "Make it sound like someone who has stories to tell.",
    "I want the voice to sound like a seasoned museum curator.",
    "Okay. I don't like how heavy it is. So I wanted Okay, let's reduce the age of the voice to mid 20s.",
    "I still want a younger voice with the same characteristics.",
])
def test_descriptive_edits_are_not_mistaken_for_conversation(text):
    import caption_edit
    assert caption_edit.is_voice_edit_intent(text)
    assert not caption_edit.is_conversation_request(text)


@pytest.mark.parametrize("text", [
    "Explain how to make it sound older.",
    "Why would you make it more calming?",
    "Don't make it older.",
])
def test_quoted_or_negated_edits_do_not_force_generation(text):
    import caption_edit
    assert not caption_edit.is_voice_edit_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "Tell me a story about an old woman.",
        "Do people get happier as they get older?",
        "I want to hear a story from France.",
        "Can you say that a little louder?",
        "I don't want the voice to be more calming.",
        "Explain why I might want the voice to be more calming.",
    ],
)
def test_conversation_is_never_prefetched_as_a_voice_edit(text):
    import caption_edit

    current = (
        "Masculine, Middle-aged. Scottish accent. Calm, gentle. "
        "A medium, velvety voice, slow."
    )
    assert not caption_edit.is_explicit_edit_request(current, text, "en")


def test_conversation_questions_are_identified_for_stale_tool_guarding():
    import caption_edit

    assert caption_edit.is_conversation_request(
        "What do you think makes a voice memorable?"
    )
    assert not caption_edit.is_conversation_request("Make the same voice raspier.")
    assert not caption_edit.is_conversation_request(
        "I want a Scottish voice of an old man who is very grumpy and rude."
    )


def test_feedback_questions_follow_the_active_language():
    assert main._feedback_question("en", 0) == "How do you like this voice?"
    assert main._feedback_question("fr", 0).startswith("Que pensez-vous")
    assert main._feedback_question("de", 0).startswith("Wie gefällt")


def test_fixed_seed_is_sent_to_every_generation(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/voice-generator/generate"):
            assert json.loads(request.content)["json_config"] == {
                "cfg_scale": 10.0,
                "seed": 1234,
            }
            return httpx.Response(
                201,
                json={"embeddings": [{"embedding_id": "vox_emb_seeded"}]},
            )
        if request.url.path.endswith("/voice-generator/embeddings"):
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {"embedding_id": "vox_emb_seeded", "ready": True}
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run():
        designer = voice_generator.VoiceDesigner(
            base_url="https://api.gradium.ai/api",
            api_key="test-key",
            store_path=tmp_path / "selections.sqlite3",
            poll_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            return await designer.generate_candidate(
                "warm and calm",
                "How do you like this voice?",
                "en",
                seed=1234,
            )
        finally:
            await designer.aclose()

    assert asyncio.run(run()) == "vox_emb_seeded"


def test_candidate_generation_retries_a_transient_connect_failure(tmp_path):
    generate_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_attempts
        if request.url.path.endswith("/voice-generator/generate"):
            generate_attempts += 1
            if generate_attempts == 1:
                raise httpx.ConnectError("temporary connection failure", request=request)
            return httpx.Response(
                201,
                json={"embeddings": [{"embedding_id": "vox_emb_retry"}]},
            )
        if request.url.path.endswith("/voice-generator/embeddings"):
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {"embedding_id": "vox_emb_retry", "ready": True}
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run():
        designer = voice_generator.VoiceDesigner(
            base_url="https://api.gradium.ai/api",
            api_key="test-key",
            store_path=tmp_path / "selections.sqlite3",
            poll_interval_s=0,
            transport=httpx.MockTransport(handler),
        )
        designer._retry_delay_s = 0
        try:
            return await designer.generate_candidate(
                "warm and calm", "How do you like this voice?", "en"
            )
        finally:
            await designer.aclose()

    assert asyncio.run(run()) == "vox_emb_retry"
    assert generate_attempts == 2


def test_duplicate_voice_conflict_is_actionable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": "A voice was already created from this embedding: existing_uid"
            },
        )

    async def run():
        designer = voice_generator.VoiceDesigner(
            base_url="https://api.gradium.ai/api",
            api_key="test-key",
            store_path=tmp_path / "selections.sqlite3",
            transport=httpx.MockTransport(handler),
        )
        try:
            await designer.keep_candidate(
                "vox_emb_duplicate",
                name="draft",
                description="warm and calm",
            )
        finally:
            await designer.aclose()

    with pytest.raises(voice_generator.DuplicateVoiceError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.existing_voice_id == "existing_uid"


def test_direct_feedback_waits_for_streaming_tts_to_finish():
    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

    async def run():
        socket = main.PlaybackAwareSocket(Socket())
        turns = []
        socket.on_user_turn = turns.append
        await socket.send_json({"type": "user_text", "text": "Make it"})
        await socket.send_json({"type": "user_text", "text": "older."})
        await socket.send_json({"type": "event", "event": "end_of_turn"})
        assert turns == ["Make it older."]
        await socket.send_json({"type": "event", "event": "first_word"})
        await socket.send_json({"type": "agent_text", "text": "One moment."})
        waiter = asyncio.create_task(socket.wait_until_agent_idle())
        await asyncio.sleep(0)
        assert not waiter.done()
        await socket.send_json({"type": "event", "event": "end_tts_audio"})
        await waiter

    asyncio.run(run())


def test_language_reset_can_suppress_internal_streaming_audio():
    class Socket:
        def __init__(self):
            self.messages = []
            self.audio = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

        async def send_bytes(self, data, *args, **kwargs):
            self.audio.append(data)

    async def run():
        raw = Socket()
        socket = main.PlaybackAwareSocket(raw)
        socket.suppress_streaming_output = True
        await socket.send_json({"type": "agent_text", "text": "internal filler"})
        await socket.send_json({"type": "audio_timing", "stop_s": 1.0})
        await socket.send_bytes(b"internal audio")
        await socket.send_json({"type": "user_text", "text": "bonjour"})
        await socket.send_json({"type": "language_changed", "language": "fr"})
        return raw

    raw = asyncio.run(run())
    assert raw.audio == []
    assert raw.messages == [
        {"type": "user_text", "text": "bonjour"},
        {"type": "language_changed", "language": "fr"},
    ]


def test_tool_acknowledgement_is_kept_but_next_llm_continuation_is_muted():
    class Socket:
        def __init__(self):
            self.messages = []
            self.audio = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

        async def send_bytes(self, data, *args, **kwargs):
            self.audio.append(data)

    async def run():
        raw = Socket()
        socket = main.PlaybackAwareSocket(raw)
        socket.suppress_on_next_llm_start()
        await socket.send_json({"type": "agent_text", "text": "Useful acknowledgement."})
        await socket.send_bytes(b"acknowledgement audio")
        await socket.send_json({"type": "event", "event": "end_tts_audio"})
        await socket.send_json({"type": "event", "event": "llm_started"})
        await socket.send_json({"type": "agent_text", "text": "One moment."})
        await socket.send_bytes(b"duplicate filler audio")
        await socket.send_json({"type": "event", "event": "end_tts_audio"})
        return raw, socket

    raw, socket = asyncio.run(run())
    assert raw.audio == [b"acknowledgement audio"]
    assert {"type": "agent_text", "text": "Useful acknowledgement."} in raw.messages
    assert not any(message.get("text") == "One moment." for message in raw.messages)
    assert socket.suppress_streaming_output


def test_direct_reply_suppression_stays_active_across_internal_tts_completions():
    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

    async def run():
        raw = Socket()
        socket = main.PlaybackAwareSocket(raw)
        socket.suppress_until_cancelled()
        await socket.send_json({"type": "event", "event": "llm_started"})
        await socket.send_json({"type": "agent_text", "text": "duplicate reply"})
        await socket.send_json({"type": "event", "event": "end_tts_audio"})
        assert socket.suppress_streaming_output
        socket.cancel_output_suppression()
        await socket.send_json({"type": "agent_text", "text": "next real reply"})
        return raw

    raw = asyncio.run(run())
    assert {"type": "agent_text", "text": "duplicate reply"} not in raw.messages
    assert raw.messages[-1] == {
        "type": "agent_text",
        "text": "next real reply",
    }


def test_deleted_draft_does_not_leave_a_stale_local_mapping(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(204)

    async def run():
        designer = voice_generator.VoiceDesigner(
            base_url="https://api.gradium.ai/api",
            api_key="test-key",
            store_path=tmp_path / "selections.sqlite3",
            transport=httpx.MockTransport(handler),
        )
        try:
            designer._save_voice(
                "vox_emb_old",
                "draft_voice",
                "draft",
                "warm and calm",
            )
            assert designer._lookup_voice("vox_emb_old") == "draft_voice"
            await designer.delete_voice("draft_voice")
            assert designer._lookup_voice("vox_emb_old") is None
        finally:
            await designer.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("revision_request", [
    "Make it older, please.",
    "Make it sound like someone who's lived and seen more life.",
    "Okay. I don't like how heavy it is. Let's reduce the age of the voice to mid 20s.",
])
def test_revision_prefetch_reuses_seed_and_resolves_tool_after_feedback(monkeypatch, revision_request):
    class FakeSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

    class FakeInput:
        def __init__(self):
            self.configs = []

        async def send_config(self, config):
            self.configs.append(config)

    class FakeHandle:
        name = "preview_voice"

        def __init__(self, description):
            self.args = {"voice_description": description}
            self.results = []

        async def send_json(self, payload):
            self.results.append(payload)

        async def send_error(self, message):
            raise AssertionError(message)

    class FakeDesigner:
        def __init__(self, **kwargs):
            self.release_first = asyncio.Event()
            self.release_second = asyncio.Event()
            self.generate_calls = []
            self.deleted_voices = []

        async def generate_candidate(
            self,
            description,
            preview_text,
            language,
            *,
            seed=None,
        ):
            self.generate_calls.append((description, language, seed))
            if len(self.generate_calls) == 1:
                await self.release_first.wait()
            elif len(self.generate_calls) == 2:
                await self.release_second.wait()
            return f"candidate-{len(self.generate_calls)}"

        async def render_preview(self, voice_id, preview_text):
            return b"not-a-wav"

        async def keep_candidate(self, embedding_id, *, name, description):
            return embedding_id.replace("candidate", "voice")

        async def delete_candidate(self, embedding_id):
            pass

        async def delete_voice(self, voice_id):
            self.deleted_voices.append(voice_id)

        async def aclose(self):
            pass

    designer = FakeDesigner()
    monkeypatch.setattr(main.voice_generator, "VoiceDesigner", lambda **kwargs: designer)

    async def fake_handle_session(
        websocket,
        *,
        on_start,
        on_config,
        on_tool_call,
        **kwargs,
    ):
        on_start({})
        input_handle = FakeInput()
        await input_handle.send_config(
            on_config(
                {
                    "type": "config",
                    "user_activity": True,
                    "user_text": (
                        "Create a feminine, middle-aged Scottish voice that is "
                        "calm and gentle."
                    ),
                }
            )
        )

        first = FakeHandle(
            "Feminine, Middle-aged. Scottish accent. Calm, gentle. "
            "A medium, velvety voice, slow."
        )
        first_task = asyncio.create_task(
            on_tool_call(first, input_handle, websocket)
        )
        while not designer.generate_calls:
            await asyncio.sleep(0)
        assert first.results == []
        assert not first_task.done()
        designer.release_first.set()
        await first_task
        assert len(first.results) == 1
        assert first.results[0]["status"] == "active"
        assert first.results[0]["feedback_spoken"] is True

        new_config = on_config(
            {
                "type": "config",
                "user_activity": True,
                "user_text": revision_request,
            }
        )
        # A safe deterministic revision owns the work immediately; it must not
        # depend on PhoneLLM emitting a second tool-call signal.
        assert new_config.tools == []
        await input_handle.send_config(new_config)
        while len(designer.generate_calls) < 2:
            await asyncio.sleep(0)
        # Supersede the still-polling revision with another spoken correction.
        # The old task must be cancelled, the lifecycle restored, and a new
        # revision started rather than leaving the session stuck in DESIGNING.
        await websocket.send_json({"type": "user_text", "text": "Make it younger."})
        await websocket.send_json({"type": "event", "event": "end_of_turn"})
        while len(designer.generate_calls) < 3:
            await asyncio.sleep(0)
        while not any(
            message.get("type") == "voice_design_status"
            and message.get("status") == "active"
            and message.get("revision") == 2
            for message in websocket.messages
        ):
            await asyncio.sleep(0)

        second = FakeHandle("older")
        await on_tool_call(second, input_handle, websocket)
        assert len(second.results) == 1
        assert second.results[0]["status"] in {"active", "already_handled"}
        assert input_handle.configs[-1].tools == []

    monkeypatch.setattr(
        main.gradbot.websocket,
        "handle_session",
        fake_handle_session,
    )

    asyncio.run(main.ws_chat(FakeSocket()))

    first_description, _, first_seed = designer.generate_calls[0]
    second_description, _, second_seed = designer.generate_calls[1]
    third_description, _, third_seed = designer.generate_calls[2]
    assert first_description.startswith("Feminine, Middle-aged")
    assert second_description.startswith("Feminine, Middle-aged")
    assert second_description.endswith(revision_request)
    assert third_description.startswith("Feminine, Middle-aged")
    assert third_description.endswith("Make it younger.")
    assert first_seed == second_seed == third_seed


def test_first_build_rebuilds_latest_correction_before_activation(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload, *args, **kwargs):
            self.messages.append(payload)

    class FakeInput:
        def __init__(self):
            self.configs = []

        async def send_config(self, config):
            self.configs.append(config)

    class FakeHandle:
        name = "preview_voice"

        def __init__(self, description):
            self.args = {"voice_description": description}
            self.results = []
            self.errors = []

        async def send_json(self, payload):
            self.results.append(payload)

        async def send_error(self, message):
            self.errors.append(message)

    class FakeDesigner:
        def __init__(self, **kwargs):
            self.first_build_started = asyncio.Event()
            self.release_first_build = asyncio.Event()
            self.generate_calls = []
            self.deleted_voices = []

        async def generate_candidate(
            self,
            description,
            preview_text,
            language,
            *,
            seed=None,
        ):
            self.generate_calls.append((description, language, seed))
            build_number = len(self.generate_calls)
            if build_number == 1:
                self.first_build_started.set()
                await self.release_first_build.wait()
            return f"candidate-{build_number}"

        async def render_preview(self, voice_id, preview_text):
            return b"not-a-wav"

        async def keep_candidate(self, embedding_id, *, name, description):
            return embedding_id.replace("candidate", "voice")

        async def delete_candidate(self, embedding_id):
            pass

        async def delete_voice(self, voice_id):
            self.deleted_voices.append(voice_id)

        async def aclose(self):
            pass

    designer = FakeDesigner()
    monkeypatch.setattr(main.voice_generator, "VoiceDesigner", lambda **kwargs: designer)

    initial_request = "Create an elderly, grumpy Scottish man's voice."
    initial_description = (
        "Masculine, elderly. Grumpy persona. Scottish accent. Low, rough, and slow."
    )
    correction = "Actually, make the same voice female."

    async def fake_handle_session(
        websocket,
        *,
        on_start,
        on_config,
        on_tool_call,
        **kwargs,
    ):
        on_start({})
        input_handle = FakeInput()
        await input_handle.send_config(
            on_config(
                {
                    "type": "config",
                    "user_activity": True,
                    "user_text": initial_request,
                }
            )
        )

        handle = FakeHandle(initial_description)
        tool_task = asyncio.create_task(
            on_tool_call(handle, input_handle, websocket)
        )
        await asyncio.wait_for(designer.first_build_started.wait(), timeout=1)

        # This is the same server-side signal sequence produced by a user who
        # corrects the request while Voice Design is still polling.
        await websocket.send_json({"type": "user_text", "text": correction})
        await websocket.send_json({"type": "event", "event": "end_of_turn"})
        designer.release_first_build.set()
        await asyncio.wait_for(tool_task, timeout=5)

        active_statuses = [
            message
            for message in websocket.messages
            if message.get("type") == "voice_design_status"
            and message.get("status") == "active"
        ]
        assert [message["voice_id"] for message in active_statuses] == ["voice-2"]
        assert handle.errors == []
        assert len(handle.results) == 1
        assert handle.results[0]["status"] == "active"
        assert handle.results[0]["voice_id"] == "voice-2"
        assert handle.results[0]["revision"] == 1
        assert "voice-1" in designer.deleted_voices

    monkeypatch.setattr(
        main.gradbot.websocket,
        "handle_session",
        fake_handle_session,
    )

    asyncio.run(main.ws_chat(FakeSocket()))

    assert len(designer.generate_calls) == 2
    first_description, first_language, first_seed = designer.generate_calls[0]
    second_description, second_language, second_seed = designer.generate_calls[1]
    assert first_description == initial_description
    assert second_description.startswith(initial_description)
    assert second_description.endswith(correction)
    assert first_language == second_language == "en"
    assert first_seed == second_seed
