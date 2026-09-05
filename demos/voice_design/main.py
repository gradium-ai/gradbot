"""Conversational Gradium voice-design demo powered by PhoneLLM."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import json
import logging
import os
import pathlib
import random
import re
from datetime import UTC, datetime

import fastapi
import gradbot
import httpx

import caption_edit
import voice_generator

APP_DIR = pathlib.Path(__file__).parent
DEFAULT_VOICE_ID = "4SZHfMpw-p46Ywgs"  # Harper
DEFAULT_LANGUAGE = "en"
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
}
LANGUAGE_ALIASES = {
    alias: code
    for code, name in LANGUAGE_NAMES.items()
    for alias in (code, name.lower())
}
SPOKEN_LANGUAGE_ALIASES = {
    "en": ("english", "anglais", "inglés", "ingles", "englisch", "inglês"),
    "fr": ("french", "français", "francais", "francés", "frances", "französisch"),
    "es": ("spanish", "espagnol", "español", "espanol", "spanisch"),
    "de": (
        "german",
        "allemand",
        "alemán",
        "aleman",
        "alemão",
        "alemao",
        "deutsch",
    ),
    "pt": (
        "portuguese",
        "portugais",
        "portugués",
        "português",
        "portugues",
    ),
}
LANGUAGE_SWITCH_CONFIRMATIONS = {
    "en": "Okay, let's continue in English.",
    "fr": "D'accord, continuons en français.",
    "es": "De acuerdo, continuemos en español.",
    "de": "Okay, sprechen wir auf Deutsch weiter.",
    "pt": "Certo, vamos continuar em português.",
}
VOICE_SELECTIONS_DB = APP_DIR / "voice-design-selections.sqlite3"
FEEDBACK_QUESTIONS = (
    "How do you like this voice?",
    "Does this feel closer to what you had in mind?",
    "What would you change about this version?",
    "Would you keep this voice, or shape it a little more?",
)
FEEDBACK_QUESTIONS_BY_LANGUAGE = {
    "en": FEEDBACK_QUESTIONS,
    "fr": (
        "Que pensez-vous de cette voix ?",
        "Cette version se rapproche-t-elle de ce que vous aviez en tête ?",
        "Que changeriez-vous dans cette version ?",
        "Voulez-vous garder cette voix ou encore l'ajuster ?",
    ),
    "es": (
        "¿Qué te parece esta voz?",
        "¿Esta versión se acerca más a lo que imaginabas?",
        "¿Qué cambiarías de esta versión?",
        "¿Quieres conservar esta voz o ajustarla un poco más?",
    ),
    "de": (
        "Wie gefällt dir diese Stimme?",
        "Kommt diese Version deiner Vorstellung näher?",
        "Was würdest du an dieser Version ändern?",
        "Möchtest du diese Stimme behalten oder noch etwas verändern?",
    ),
    "pt": (
        "O que você acha desta voz?",
        "Esta versão está mais perto do que você imaginou?",
        "O que você mudaria nesta versão?",
        "Quer ficar com esta voz ou ajustá-la mais um pouco?",
    ),
}
TOO_SUBTLE_QUESTIONS = {
    "en": "That change sounded too close to the current voice. What should I push further?",
    "fr": "Ce changement ressemble trop à la voix actuelle. Que dois-je accentuer davantage ?",
    "es": "Ese cambio suena demasiado parecido a la voz actual. ¿Qué debería acentuar más?",
    "de": "Diese Änderung klingt der aktuellen Stimme zu ähnlich. Was soll ich stärker verändern?",
    "pt": "Essa mudança ficou parecida demais com a voz atual. O que devo acentuar mais?",
}
ACKNOWLEDGEMENTS = (
    "Let me shape that carefully.",
    "I'll try that direction.",
    "Let's hear what that does.",
    "I'll make that adjustment.",
    "I have the change in mind.",
    "Let's give that nuance room.",
)
ACKNOWLEDGEMENTS_BY_LANGUAGE = {
    "en": ACKNOWLEDGEMENTS,
    "fr": (
        "Laissez-moi ajuster ça.",
        "Je vais essayer cette direction.",
        "Écoutons ce que cela donne.",
        "Je vais faire cet ajustement.",
        "J'ai bien le changement en tête.",
        "Laissons cette nuance s'exprimer.",
    ),
    "es": (
        "Déjame ajustar eso.",
        "Probaré esa dirección.",
        "Veamos cómo suena.",
        "Haré ese ajuste.",
        "Tengo claro el cambio.",
        "Démosle espacio a ese matiz.",
    ),
    "de": (
        "Ich passe das behutsam an.",
        "Ich probiere diese Richtung.",
        "Hören wir, was das bewirkt.",
        "Ich nehme diese Anpassung vor.",
        "Ich habe die Änderung im Kopf.",
        "Geben wir dieser Nuance Raum.",
    ),
    "pt": (
        "Vou ajustar isso com cuidado.",
        "Vou experimentar essa direção.",
        "Vamos ouvir como fica.",
        "Vou fazer esse ajuste.",
        "Entendi bem a mudança.",
        "Vamos dar espaço a essa nuance.",
    ),
}
PREFETCH_CLAIM_TIMEOUT_S = float(
    os.getenv("VOICE_DESIGN_PREFETCH_CLAIM_TIMEOUT_S", "15")
)

# Keep Gradbot's streaming TTS on the model used by the Voice Design workflow.
os.environ.setdefault("GRADIUM_TTS_MODEL_NAME", voice_generator.TTS_MODEL_NAME)

gradbot.init_logging()
logger = logging.getLogger(__name__)
app = fastapi.FastAPI(title="Gradbot Voice Workshop")
config_path = APP_DIR / "config.yaml"
if not config_path.exists():
    config_path = APP_DIR / "config.example.yaml"
cfg = gradbot.config.load(config_path)


@dataclasses.dataclass
class VoiceDesignState:
    """Mutable state scoped to one WebSocket conversation."""

    voice_id: str = DEFAULT_VOICE_ID
    agent_voice_id: str = DEFAULT_VOICE_ID
    draft_voice_id: str | None = None
    saved_voice_ids: set[str] = dataclasses.field(default_factory=set)
    language: str = DEFAULT_LANGUAGE
    description: str = ""
    seed: int | None = None
    revision: int = 0
    finalized: bool = False
    speed: float = 1.0
    design_in_progress: bool = False
    preview_armed: bool = True
    latest_user_text: str = ""
    retired_voice_ids: list[str] = dataclasses.field(default_factory=list)
    prefetch_task: asyncio.Task | None = None
    prefetch_claim: asyncio.Event | None = None
    prefetch_claimed: bool = False
    prefetch_description: str = ""
    prefetch_user_text: str = ""
    prefetch_turn_sequence: int = -1
    turn_sequence: int = 0
    language_switch_in_progress: bool = False
    direct_response_in_progress: bool = False
    language_tool_handled: bool = False
    turn_route: str | None = None
    pending_language: str | None = None
    language_route_armed: bool = False

    @property
    def lang(self):
        return (
            gradbot.LANGUAGES.get(self.language) or gradbot.LANGUAGES[DEFAULT_LANGUAGE]
        )


TOOLS = [
    gradbot.ToolDef(
        "preview_voice",
        (
            "Create a new Gradium voice and make it the live conversation voice. "
            "Call only when the latest utterance asks to create a voice or change "
            "how the active voice sounds. Terse relative feedback such as 'warmer' "
            "or 'older' counts as a change. Never call for an informational or "
            "discussion question merely because it mentions voices."
        ),
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "voice_description": {
                        "type": "string",
                        "description": (
                            "For the first draft, write a concise English voice caption "
                            "in this order: '<Gender>, <age>. <Persona>. <Accent>. "
                            "<Emotion>. <How the voice sounds.>', omitting unsupported "
                            "slots. For revisions, send only the user's latest requested "
                            "change; the app applies common one-trait edits in place and "
                            "preserves the rest. Maximum 500 characters."
                        ),
                    },
                },
                "required": ["voice_description"],
            }
        ),
    ),
    gradbot.ToolDef(
        "finalize_voice",
        (
            "Keep the current preview as the chosen voice. Call only after the user "
            "clearly says they approve, want to keep it, take it, save it, use it, "
            "or finalize it."
        ),
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "voice_name": {
                        "type": "string",
                        "description": (
                            "Optional short name requested by the user for the "
                            "permanent voice. Omit when they did not provide one."
                        ),
                    }
                },
                "required": [],
            }
        ),
    ),
    gradbot.ToolDef(
        "switch_conversation_voice",
        (
            "Switch the live conversation between the original agent voice and the "
            "latest designed voice. Call only when the user explicitly requests one "
            "of those two voice sources. Never use this tool to change the spoken "
            "language, accent, gender, or other voice traits."
        ),
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["agent", "designed"],
                        "description": "The voice the user explicitly wants to hear.",
                    }
                },
                "required": ["target"],
            }
        ),
    ),
    gradbot.ToolDef(
        "switch_language",
        (
            "The only permitted way to change the spoken conversation language. "
            "Call only for an explicit command such as 'speak French', 'switch to "
            "French', or 'continue in French'. A language used as a voice trait—for "
            "example 'French voice', 'French accent', or 'French-accented woman'—is "
            "a voice-design request, not a language switch. This updates the "
            "assistant, speech recognition, speech synthesis, and future voice designs."
        ),
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": list(LANGUAGE_NAMES),
                        "description": (
                            "Language code: en for English, fr for French, es for "
                            "Spanish, de for German, or pt for Portuguese."
                        ),
                    }
                },
                "required": ["language"],
            }
        ),
    ),
]


def _append_voice_changes(current: str, requested_changes: str) -> str:
    """Preserve prior direction while making the latest request authoritative."""

    changes = requested_changes.strip()
    if not changes:
        raise voice_generator.VoiceDesignError("Describe the voice change to make")
    if len(changes) > voice_generator.MAX_DESCRIPTION_CHARS:
        raise voice_generator.VoiceDesignError(
            f"Voice changes can be at most {voice_generator.MAX_DESCRIPTION_CHARS} characters"
        )
    if not current.strip():
        return changes

    # PhoneLLM sometimes sends the full consolidated description even though the
    # tool asks for only the latest delta. Treat that as replacement input rather
    # than appending the same traits indefinitely.
    current_normalized = " ".join(current.casefold().split())
    changes_normalized = " ".join(changes.casefold().split())
    if changes_normalized == current_normalized:
        return current.strip()
    if current_normalized in changes_normalized:
        return changes

    marker = " Latest requested changes (override any conflicts): "
    available = (
        voice_generator.MAX_DESCRIPTION_CHARS - len(marker) - len(changes) - 1
    )
    if available <= 0:
        return changes
    base = current.strip()[:available].rstrip(" ,.;:")
    return f"{base}.{marker}{changes}" if base else changes


def _resolve_voice_description(
    current: str,
    requested_changes: str,
    user_text: str,
    language: str,
) -> str:
    """Preserve the prior prompt and append the user's complete latest change."""

    if current and user_text:
        return _append_voice_changes(current, user_text)
    return _append_voice_changes(current, requested_changes)


def _feedback_question(language: str, revision: int) -> str:
    questions = FEEDBACK_QUESTIONS_BY_LANGUAGE.get(language, FEEDBACK_QUESTIONS)
    return questions[max(0, revision) % len(questions)]


def _is_explicit_language_switch(user_text: str, language: str) -> bool:
    """Return whether the user explicitly asked to change conversation language."""

    language_names = SPOKEN_LANGUAGE_ALIASES.get(language, ())
    if not language_names:
        return False
    text = " ".join(re.sub(r"[^\w]+", " ", user_text.casefold()).split())
    target = next((name for name in language_names if name in text.split()), None)
    if not text or target is None:
        return False

    # Language names that describe a voice or accent belong to Voice Design.
    # Requiring an explicit conversation verb keeps requests such as "a French
    # voice" and "a French-accented man" away from switch_language.
    language_pattern = re.escape(target)
    explicit_patterns = (
        rf"\b(?:speak|talk)\s+(?:to\s+me\s+in\s+|in\s+)?{language_pattern}\b",
        rf"\b(?:switch|change)\s+(?:the\s+)?(?:conversation\s+)?(?:language\s+)?to\s+{language_pattern}\b",
        rf"\b(?:continue|answer|respond|reply|say\s+it)\b(?:\s+\w+){{0,4}}\s+in\s+{language_pattern}\b",
        rf"\b(?:use)\s+{language_pattern}(?:\s+from\s+now\s+on|\s+instead)?\b",
        rf"\b(?:parle|parlez|parler|passe|passez|passer|change|changez|changer|continue|continuez|continuer|réponds|reponds|répondez|repondez)\b(?:\s+\w+){{0,6}}\s+(?:en\s+|à\s+|a\s+)?{language_pattern}\b",
        rf"\b(?:habla|hable|cambia|cambie|continúa|continua|responde|responda)\b(?:\s+\w+){{0,6}}\s+(?:en\s+|a\s+)?{language_pattern}\b",
        rf"\b(?:sprich|sprechen|wechsle|wechseln|ändere|andere|antworte)\b(?:\s+\w+){{0,6}}\s+(?:auf\s+|zu\s+)?{language_pattern}\b",
        rf"\b(?:fale|falar|mude|mudar|continue|responda|responder)\b(?:\s+\w+){{0,6}}\s+(?:em\s+|para\s+)?{language_pattern}\b",
    )
    return any(re.search(pattern, text) for pattern in explicit_patterns)


def _requested_language_switch(user_text: str) -> str | None:
    """Extract an explicit supported conversation-language request, if any."""

    return next(
        (
            language
            for language in LANGUAGE_NAMES
            if _is_explicit_language_switch(user_text, language)
        ),
        None,
    )


def _requested_voice_source(user_text: str) -> str | None:
    """Extract an explicit request for the agent or latest designed voice."""

    text = " ".join(re.sub(r"[^\w]+", " ", user_text.casefold()).split())
    if not text:
        return None
    action = r"(?:use|switch|change|hear|play|return|go|back|revert|utilise|utiliser|passe|reviens|retourne)"
    if re.search(
        rf"\b{action}\b(?:\s+\w+){{0,8}}\s+(?:original|real|agent|harper|originale)\b(?:\s+\w+){{0,3}}\s+(?:voice|voix)\b",
        text,
    ) or re.search(
        rf"\b{action}\b(?:\s+\w+){{0,8}}\s+(?:voice|voix)\b(?:\s+\w+){{0,3}}\s+(?:original|real|agent|harper|originale)\b",
        text,
    ):
        return "agent"
    if re.search(
        rf"\b{action}\b(?:\s+\w+){{0,8}}\s+(?:design(?:ed)?|created|draft|latest|new|créée|creee|conçue|concue)\b(?:\s+\w+){{0,3}}\s+(?:voice|voix)\b",
        text,
    ) or re.search(
        rf"\b{action}\b(?:\s+\w+){{0,8}}\s+(?:voice|voix)\b(?:\s+\w+){{0,3}}\s+(?:design(?:ed)?|created|draft|latest|new|créée|creee|conçue|concue)\b",
        text,
    ):
        return "designed"
    return None


def _system_prompt(state: VoiceDesignState) -> str:
    current = state.description or "the selected starter voice; no custom draft yet"
    phase = (
        "DESIGNING"
        if state.design_in_progress
        else "FINALIZED"
        if state.finalized
        else "AWAITING_FEEDBACK"
        if state.draft_voice_id
        else "DRAFTING"
    )
    active_voice = (
        "the original agent voice"
        if state.voice_id == state.agent_voice_id
        else "the latest designed voice"
    )
    acknowledgements = ACKNOWLEDGEMENTS_BY_LANGUAGE.get(
        state.language, ACKNOWLEDGEMENTS
    )
    suggested_acknowledgement = acknowledgements[
        state.revision % len(acknowledgements)
    ]
    language_name = LANGUAGE_NAMES[state.language]

    if state.language_switch_in_progress:
        return f"""The conversation language has just changed to {language_name}.

The localized confirmation is played directly by the application. Output no spoken
text. You must call reset_asr exactly once now and call no other function. After ASR is reset,
output no tokens and make no function calls. Never write a placeholder such as
"[Empty response]".
"""

    if state.direct_response_in_progress:
        return f"""A short confirmation is being played directly by the application.

Output no spoken text, make no function calls, and never write a placeholder such as
"[Empty response]", "empty", or "silence". Wait for the user's next real utterance.
The conversation language is {language_name}.
"""

    # Gradbot injects an empty continuation with a PENDING result when a tool takes
    # longer than 1.5 seconds. Keep that continuation deliberately sterile: the
    # full workflow prompt contains function names and can make PhoneLLM repeat the
    # outstanding call or generate the same holding phrase indefinitely.
    if state.design_in_progress:
        return f"""You are waiting for a background voice operation to finish.

The operation is already running. For every empty user message or pending result,
return an EMPTY response unless the newest result contains a holding_phrase. If it
does, say that phrase exactly once and say nothing else. Make no function calls and
never invent, paraphrase, or repeat a progress update. Output no placeholder such as
"[Empty response]", "empty", or "silence". Then wait silently for the final result.
The conversation language is {language_name}.
"""

    if state.turn_route == "conversation":
        return f"""You are a concise, friendly voice design director in a live call.

The user's latest utterance is an ordinary informational or discussion request, not
a request to create or alter a voice. Answer it directly and naturally in
{language_name}, using one or two short sentences. Call no tools. Do not use a design
acknowledgement, defer the answer, or mention a system, process, or unfinished work.
"""

    if state.turn_route == "language" and state.pending_language:
        requested_name = LANGUAGE_NAMES[state.pending_language]
        return f"""The user explicitly asked to switch the conversation to {requested_name}.

Call switch_language now with language={state.pending_language!r}. Output no spoken
text and call no other tool. The application will play the confirmation.
"""

    if state.turn_route == "revision":
        return f"""You are revising the active designed voice from the user's latest utterance.

Speak only {language_name}. Say exactly "{suggested_acknowledgement}" once, then call
preview_voice with only the latest requested change. Terse relative feedback is valid.
Do not ask a question, add a second filler, or discuss the process.
"""

    # The final tool result arrives as another empty continuation. It already
    # contains the exact question to speak. No app tools are needed until actual
    # user speech re-arms the workflow.
    if state.draft_voice_id and not state.preview_armed:
        return f"""You are a concise voice design director.

A newly designed voice is active. Its feedback question is played directly in that
voice. When a completed result says feedback_spoken is true, return an EMPTY response:
do not repeat or paraphrase the question. If the result says no new spoken request was
received, return an EMPTY response. On any other empty message, return an EMPTY
response. Output no placeholder such as "[Empty response]", "empty", or "silence".
Make no function calls until the user actually speaks. Speak {language_name}.
"""

    return f"""You are a concise voice design director in a live audio workshop.

Your job is to help the user create a voice by talking naturally, previewing each
revision through the Gradium Voice Design API, and keeping it only on approval.

MANDATORY INTENT ROUTING:
Classify only the latest user utterance before responding. An informational or
discussion request—even if it mentions voices or voice traits—is ordinary
conversation: answer it directly, call no tool, and never use a design
acknowledgement. Only a request to create a voice or change how the active voice
sounds is a design request. A polite question explicitly asking for a change, such
as "Could you make it warmer?", and terse feedback such as "Warmer, please" are
still design requests.

MANDATORY LANGUAGE ROUTING:
Only call switch_language for an explicit conversation-language command such as
"speak French", "switch to French", or "continue in French". A language that
modifies a requested voice, accent, speaker, or pronunciation is a VOICE TRAIT:
"French voice", "French accent", and "French-accented woman" must use preview_voice
and must not change the conversation language. After switch_language succeeds, the
application plays the localized confirmation directly; say nothing while the app
performs its one internal reset_asr continuation.
The special [start] message is never a language request: on [start], call no tools;
only greet the user and ask what kind of voice they want to create.

Current phase: {phase}
Current voice description: {current}
Current revision: {state.revision}
Current conversation voice: {active_voice}
Suggested acknowledgement for the next revision: {suggested_acknowledgement}
Conversation language: {language_name} ({state.language})

RULES — never break these:
1. Keep every spoken response to 1-2 short sentences.
2. A single concrete trait—such as gender, accent, age, tone, energy, pitch, pace,
   or texture—is enough to create a first preview. Do not ask for more detail when
   the user has already supplied any usable trait, and never repeat a question.
3. When the user requests a voice or a change, call preview_voice immediately in
   the same turn. Before the call, use at most one mindful acknowledgement of 3-7
   words. Do not paraphrase the request, praise it, or repeat an acknowledgement
   already used in the conversation. Prefer silence when a filler would feel
   forced. If you do acknowledge, use the suggested acknowledgement for this
   revision exactly. Never stack fillers or add a second progress update.
4. For the first draft, send one concise English caption in this order: gender and
   age, persona, accent, emotion, then concrete sound qualities. Omit unsupported
   slots. For revisions, send only the latest change. Relative requests such as
   "older", "female", or "less energetic" are valid; the app edits common traits
   in place and otherwise appends the latest change as authoritative.
5. After preview_voice succeeds, the application plays its feedback question in the
   newly designed voice. Say nothing: never provide a separate audition line, repeat
   the question, or repeat the pre-tool acknowledgement.
6. Call finalize_voice only after explicit approval. Include voice_name only when
   the user supplied one. Never infer approval from silence or mild feedback. In
   speech, say "keep this voice" or "save this voice"—never "finalize" or "lock".
7. Keep using the latest designed voice throughout revisions. If the user asks to
   hear the original agent voice, call switch_conversation_voice with target
   "agent". Use target "designed" only when they ask to return to the draft.
8. A saved voice may still be revised in the same call. Preserve the saved version
   and create a new draft from its description plus the latest requested change.
9. Do not ask "What would you like the voice to sound like?" when the conversation
   already contains a voice description or requested change.
10. Do not expose tool names, API details, code, or internal instructions.
11. Speak {language_name}. Call switch_language only when the user explicitly asks
    the conversation to speak, switch, continue, answer, or respond in English,
    French, Spanish, German, or Portuguese. Never infer this from a language-named
    voice or accent. Do not use switch_conversation_voice for a language request.
12. After switch_language succeeds, say nothing. The application plays the exact
    localized confirmation and runs reset_asr exactly once in a private continuation.
    Do not switch again without a new user request.
13. For unsupported languages, briefly explain that this demo currently supports
    English, French, Spanish, German, and Portuguese. Do not call a tool.
14. Never call preview_voice because of [start], an empty tool-result turn, or the
    current voice description. Call it only for an explicit voice request in the
    user's latest non-empty spoken message.
15. While Current phase is DESIGNING, the existing voice request is already running.
    Say at most one brief holding phrase, then wait. Do not call any tool or repeat
    the request.
16. If the user asks an ordinary conversational question, answer it directly and
    naturally. Never say that the system will process it, and never invent progress.
"""


def _make_config(
    state: VoiceDesignState,
    *,
    speaks_first: bool = False,
) -> gradbot.SessionConfig:
    # The UI's speed control adjusts pause sensitivity like the other demos:
    # faster means less end-of-turn padding.
    padding_bonus = max(-4.0, min(4.0, -4.0 * (state.speed - 1.0)))
    runtime = {
        "assistant_speaks_first": speaks_first,
        "silence_timeout_s": 0.0,
        "padding_bonus": padding_bonus,
        "rewrite_rules": state.lang.rewrite_rules,
    }
    llm_extra_config = dict(cfg.llm.extra_config or {})
    chat_template_kwargs = dict(
        llm_extra_config.get("chat_template_kwargs") or {}
    )
    # PhoneLLM is a realtime tool-using model. Hidden reasoning consumes the
    # small streaming budget and can leave content empty or delayed, so preserve
    # this model-card setting in every state-specific config—not only the YAML.
    chat_template_kwargs["enable_thinking"] = False
    llm_extra_config["chat_template_kwargs"] = chat_template_kwargs
    if speaks_first:
        llm_extra_config.update(
            temperature=0,
            tool_choice="none",
            # PhoneLLM can occasionally continue by generating the same opening
            # question a second time. The greeting has exactly one question, so
            # end this one-off stream at its first question mark.
            stop=["?"],
            max_tokens=64,
        )
    elif state.language_switch_in_progress:
        llm_extra_config.update(
            temperature=0,
            # Gradbot always adds its private reset_asr tool to the model's tool
            # list. Force that one silent continuation: merely asking PhoneLLM
            # to choose it proved nondeterministic in live multilingual runs.
            tool_choice={
                "type": "function",
                "function": {"name": "reset_asr"},
            },
            stop=["[Empty response]", "[Empty", "Empty response"],
            max_tokens=32,
        )
    elif state.direct_response_in_progress:
        llm_extra_config.update(
            temperature=0,
            tool_choice="none",
            stop=["[Empty response]", "[Empty", "Empty response"],
            max_tokens=1,
        )
    elif state.design_in_progress or not state.preview_armed:
        # These are internal continuation turns, never fresh user requests. Force
        # native tool selection off even though Gradbot advertises its internal
        # reset_asr helper. This keeps older Gradbot builds safe too: PhoneLLM
        # cannot turn a PENDING/result continuation into another preview call.
        llm_extra_config.update(
            tool_choice="none",
            # PhoneLLM can turn a request for no output into the literal marker
            # "[Empty response]". Stop it before that marker reaches TTS.
            stop=["[Empty response]", "[Empty", "Empty response"],
            max_tokens=1,
        )
        if state.design_in_progress:
            # The silent prompt should normally produce no tokens. This cap is a
            # hard backstop against repeated filler text within one stream.
            llm_extra_config["temperature"] = 0
    elif state.turn_route == "conversation":
        llm_extra_config.update(
            temperature=0,
            tool_choice="none",
            max_tokens=96,
        )
    elif state.turn_route == "language" and state.pending_language:
        llm_extra_config.update(
            temperature=0,
            tool_choice={
                "type": "function",
                "function": {"name": "switch_language"},
            },
            max_tokens=48,
        )
    elif state.turn_route == "revision":
        llm_extra_config.update(
            temperature=0,
            tool_choice={"type": "function", "function": {"name": "preview_voice"}},
            max_tokens=96,
        )
    runtime["llm_extra_config"] = json.dumps(llm_extra_config)
    # A long Voice Design request causes Gradbot to ask PhoneLLM for a holding
    # phrase with a PENDING tool result. Temporarily removing app tools makes that
    # generation speech-only, preventing PhoneLLM from recursively starting the
    # same tool again every 1.5 seconds.
    if (
        speaks_first
        or state.language_switch_in_progress
        or state.direct_response_in_progress
        or state.design_in_progress
        or not state.preview_armed
        or state.turn_route == "conversation"
    ):
        tools = []
    elif state.turn_route == "language":
        tools = [tool for tool in TOOLS if tool.name == "switch_language"]
    elif state.turn_route == "revision":
        tools = [tool for tool in TOOLS if tool.name == "preview_voice"]
    else:
        tools = TOOLS
    return gradbot.SessionConfig(
        voice_id=state.voice_id,
        language=state.lang,
        instructions=_system_prompt(state),
        tools=tools,
        **(cfg.session_kwargs | runtime),
    )


class PlaybackAwareSocket:
    """Track Gradbot speech so direct feedback never talks over a filler."""

    def __init__(self, websocket: fastapi.WebSocket) -> None:
        self._websocket = websocket
        self._agent_idle = asyncio.Event()
        self._agent_idle.set()
        self._user_words: list[str] = []
        self.on_user_turn = None
        self.on_user_text = None
        self.on_llm_started = None
        self.suppress_streaming_output = False
        self._release_suppression_on_tts_end = False
        self._suppression_saw_llm_start = False
        self._suppression_generation = 0
        self._suppress_on_next_llm_start = False

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    @property
    def current_user_text(self) -> str:
        return " ".join(self._user_words).strip()

    async def send_json(self, payload, *args, **kwargs):
        if isinstance(payload, dict):
            kind = payload.get("type")
            was_suppressed = self.suppress_streaming_output
            if kind == "event" and payload.get("event") in {
                "first_word",
                "first_tts_audio",
            }:
                self._agent_idle.clear()
            elif kind == "user_text":
                text = str(payload.get("text") or "").strip()
                if text:
                    self._user_words.append(text)
                    if self.on_user_text is not None:
                        try:
                            observed = self.on_user_text(self.current_user_text)
                            if inspect.isawaitable(observed):
                                await observed
                        except Exception:
                            logger.exception("partial user-text observer failed")
            elif kind == "event" and payload.get("event") == "end_tts_audio":
                self._agent_idle.set()
            elif kind == "event" and payload.get("event") == "end_of_turn":
                utterance = " ".join(self._user_words).strip()
                self._user_words.clear()
                # A deferred mute belongs only to the tool continuation from the
                # turn that armed it. Never carry it into a new user response.
                self._suppress_on_next_llm_start = False
                if was_suppressed:
                    # A new user turn is an unconditional boundary. Never let a
                    # missing/empty private continuation mute their real reply.
                    self.cancel_output_suppression()
                if utterance and self.on_user_turn is not None:
                    try:
                        observed = self.on_user_turn(utterance)
                        if inspect.isawaitable(observed):
                            await observed
                    except Exception:
                        logger.exception("user-turn observer failed")
            if (
                kind == "event"
                and payload.get("event") == "llm_started"
                and self.on_llm_started is not None
            ):
                try:
                    observed = self.on_llm_started()
                    if inspect.isawaitable(observed):
                        await observed
                except Exception:
                    logger.exception("LLM-start observer failed")
            if (
                kind == "event"
                and payload.get("event") == "llm_started"
                and self._suppress_on_next_llm_start
            ):
                # The valid acknowledgement belonged to the tool-calling stream.
                # Hide any later PENDING/terminal continuation so PhoneLLM cannot
                # stack "One moment" or repeat the acknowledgement while building.
                self.suppress_until_tts_end()
            if (
                self.suppress_streaming_output
                and kind == "event"
                and payload.get("event") == "llm_started"
            ):
                self._suppression_saw_llm_start = True
            if self.suppress_streaming_output and (
                kind in {"agent_text", "audio_timing"}
                or kind == "event"
                and payload.get("event")
                in {"first_word", "first_tts_audio", "end_tts_audio"}
            ):
                if (
                    kind == "event"
                    and payload.get("event") == "end_tts_audio"
                    and self._release_suppression_on_tts_end
                    and self._suppression_saw_llm_start
                ):
                    generation = self._suppression_generation
                    self._release_suppression_on_tts_end = False
                    self._suppression_saw_llm_start = False
                    asyncio.create_task(
                        self._finish_suppression_after_drain(generation)
                    )
                return
        await self._websocket.send_json(payload, *args, **kwargs)

    async def send_bytes(self, data, *args, **kwargs):
        if self.suppress_streaming_output:
            return
        await self._websocket.send_bytes(data, *args, **kwargs)

    async def wait_until_agent_idle(self) -> None:
        if self._agent_idle.is_set():
            return
        try:
            await asyncio.wait_for(self._agent_idle.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("Timed out waiting for current TTS before direct feedback")

    def suppress_until_tts_end(self) -> None:
        self._suppression_generation += 1
        self._suppress_on_next_llm_start = False
        self.suppress_streaming_output = True
        self._release_suppression_on_tts_end = True
        self._suppression_saw_llm_start = False

    def suppress_until_cancelled(self) -> None:
        """Mute every internal stream until an app-owned reply has finished."""

        self._suppression_generation += 1
        self._suppress_on_next_llm_start = False
        self.suppress_streaming_output = True
        self._release_suppression_on_tts_end = False
        self._suppression_saw_llm_start = False

    def cancel_output_suppression(self) -> None:
        self._suppression_generation += 1
        self._suppress_on_next_llm_start = False
        self.suppress_streaming_output = False
        self._release_suppression_on_tts_end = False
        self._suppression_saw_llm_start = False

    def suppress_on_next_llm_start(self) -> None:
        """Allow the current acknowledgement, then mute its tool continuation."""

        self._suppress_on_next_llm_start = True

    async def _finish_suppression_after_drain(self, generation: int) -> None:
        # Gradbot's EndTTS event can precede its final queued caption/audio chunk.
        # Keep the private continuation muted through a short playback drain.
        await asyncio.sleep(1.0)
        if (
            generation != self._suppression_generation
            or not self.suppress_streaming_output
        ):
            return
        self.cancel_output_suppression()
        try:
            await self._websocket.send_json(
                {"type": "event", "event": "internal_continuation_done"}
            )
        except Exception:
            logger.debug("Socket closed while releasing output suppression")


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket) -> None:
    watched_socket = PlaybackAwareSocket(websocket)
    state = VoiceDesignState()
    build_lock = asyncio.Lock()
    session_input_handle = None
    api_key = cfg.gradium.api_key.get_secret_value() if cfg.gradium.api_key else ""
    designer = voice_generator.VoiceDesigner(
        base_url=str(cfg.gradium.base_url or "https://api.gradium.ai/api"),
        api_key=api_key,
        store_path=VOICE_SELECTIONS_DB,
        http_timeout_s=float(os.getenv("VOICE_DESIGN_HTTP_TIMEOUT_S", "120")),
        poll_timeout_s=float(os.getenv("VOICE_DESIGN_POLL_TIMEOUT_S", "120")),
        poll_interval_s=float(os.getenv("VOICE_DESIGN_POLL_INTERVAL_S", "1")),
    )

    async def flush_retired_voices() -> None:
        retired, state.retired_voice_ids = state.retired_voice_ids, []
        for voice_id in retired:
            if voice_id != state.voice_id and voice_id not in state.saved_voice_ids:
                await designer.delete_voice(voice_id)

    async def build_artifacts(
        description: str,
        feedback_question: str,
        seed: int,
    ) -> tuple[str, bytes]:
        """Generate, audition, then promote in the order required by the API."""

        async with build_lock:
            candidate_id = await designer.generate_candidate(
                description,
                feedback_question,
                state.language,
                seed=seed,
            )
            promoted_voice_id = None
            try:
                # Keeping a candidate clears its temporary embedding. Rendering
                # and promoting concurrently therefore races /speech/tts against
                # /voices/from-embedding and intermittently produces a 404.
                feedback_audio = await designer.render_preview(
                    candidate_id,
                    feedback_question,
                )
                promoted_voice_id = await designer.keep_candidate(
                    candidate_id,
                    name=f"Voice Workshop Draft {state.revision + 1}",
                    description=description,
                )
            except BaseException:
                if promoted_voice_id:
                    await designer.delete_voice(promoted_voice_id)
                await designer.delete_candidate(candidate_id)
                raise
            await designer.delete_candidate(candidate_id)
            return promoted_voice_id, feedback_audio

    async def send_feedback_audio(
        ws,
        *,
        text: str,
        audio: bytes,
        revision: int,
        wait_for_stream: bool = True,
    ) -> None:
        if wait_for_stream and isinstance(ws, PlaybackAwareSocket):
            # Give a just-started Gradbot stream time to publish FirstTTS before
            # deciding it is idle. This closes the race between a tool callback
            # and its pre-tool acknowledgement reaching the audio queue.
            await asyncio.sleep(0.15)
            await ws.wait_until_agent_idle()
            # EndTTS can arrive just ahead of the last queued audio packet. This
            # margin prevents the app-owned created voice from overlapping it.
            await asyncio.sleep(0.6)
        duration_s = voice_generator.wav_duration_s(audio)
        await ws.send_json(
            {
                "type": "voice_feedback_audio",
                "text": text,
                "mime_type": "audio/wav",
                "audio_base64": base64.b64encode(audio).decode("ascii"),
                "duration_s": duration_s,
                "revision": revision,
            }
        )
        await asyncio.sleep(min(duration_s + 0.25, 10.0))

    async def play_direct_confirmation(
        *,
        text: str,
        input_handle,
        ws,
    ) -> None:
        """Play one app-owned reply while hiding empty tool continuations."""

        try:
            audio = await designer.render_preview(state.voice_id, text)
            await send_feedback_audio(
                ws,
                text=text,
                audio=audio,
                revision=state.revision,
                wait_for_stream=True,
            )
            # Let any internal tool-result continuation finish behind the output
            # gate before normal streaming speech is restored.
            await asyncio.sleep(0.5)
        except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
            await ws.send_json(
                {
                    "type": "error",
                    "message": f"The action succeeded, but its confirmation failed: {exc}",
                }
            )
        finally:
            state.language_switch_in_progress = False
            state.direct_response_in_progress = False
            try:
                await input_handle.send_config(_make_config(state))
            finally:
                if isinstance(ws, PlaybackAwareSocket):
                    ws.cancel_output_suppression()

    async def activate_voice(
        *,
        description: str,
        seed: int,
        new_voice_id: str,
        feedback_question: str,
        feedback_audio: bytes,
        input_handle,
        ws,
    ) -> None:
        previous_draft_voice_id = state.draft_voice_id
        state.draft_voice_id = new_voice_id
        state.voice_id = new_voice_id
        state.description = description
        state.seed = seed
        state.revision += 1
        state.finalized = False
        state.design_in_progress = False
        state.preview_armed = False
        state.turn_route = None
        state.pending_language = None
        state.language_route_armed = False
        if (
            previous_draft_voice_id
            and previous_draft_voice_id != new_voice_id
            and previous_draft_voice_id not in state.saved_voice_ids
        ):
            state.retired_voice_ids.append(previous_draft_voice_id)

        await input_handle.send_config(_make_config(state))
        await ws.send_json(
            {
                "type": "voice_design_status",
                "status": "active",
                "voice_id": new_voice_id,
                "description": description,
                "revision": state.revision,
                "seed": seed,
            }
        )
        await send_feedback_audio(
            ws,
            text=feedback_question,
            audio=feedback_audio,
            revision=state.revision,
        )
        await flush_retired_voices()

    def clear_prefetch(task: asyncio.Task | None = None) -> None:
        if task is not None and state.prefetch_task is not task:
            return
        state.prefetch_task = None
        state.prefetch_claim = None
        state.prefetch_claimed = False
        state.prefetch_description = ""
        state.prefetch_user_text = ""
        state.prefetch_turn_sequence = -1

    async def cancel_prefetch(reason: str) -> None:
        """Cancel obsolete speculative work before handling a different tool."""

        task = state.prefetch_task
        if task is None:
            return
        logger.info("Cancelling revision prefetch: %s", reason)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        clear_prefetch(task)

    async def settle_ignored_tool(handle, input_handle, reason: str) -> None:
        """Resolve a stale tool call without allowing a continuation loop."""

        state.preview_armed = False
        await input_handle.send_config(_make_config(state))
        watched_socket.suppress_until_tts_end()
        await handle.send_json(
            {
                "success": True,
                "status": "already_handled",
                "message": (
                    f"No new action is needed ({reason}). Return an empty response "
                    "and wait for the user's next utterance."
                ),
            }
        )

    def start_revision_prefetch(user_text: str) -> None:
        """Begin only high-confidence deterministic edits at end-of-speech."""

        if (
            session_input_handle is None
            or state.design_in_progress
            or state.prefetch_task is not None
            or not state.draft_voice_id
            or not (caption_edit.is_voice_edit_intent(user_text) or caption_edit.is_explicit_edit_request(
                state.description,
                user_text,
                state.language,
            ))
        ):
            return
        # Explicit edits need not fit the finite caption-rewriter vocabulary.
        # Own the operation here so a malformed/missing LLM tool call cannot
        # leave a promised revision idle. The complete utterance is the delta.
        # Preserve every requested trait in compound revisions (for example
        # "older, raspier, and slower") instead of keeping only the first
        # deterministic ladder edit.
        description = _append_voice_changes(state.description, user_text)

        seed = state.seed if state.seed is not None else random.randint(
            0, voice_generator.MAX_SEED
        )
        feedback_question = _feedback_question(state.language, state.revision)
        claim = asyncio.Event()
        state.prefetch_claim = claim
        state.prefetch_claimed = False
        state.prefetch_description = description
        state.prefetch_user_text = user_text
        state.prefetch_turn_sequence = state.turn_sequence
        state.design_in_progress = True
        state.preview_armed = False

        async def run_prefetch():
            this_task = asyncio.current_task()
            artifacts = None
            try:
                await session_input_handle.send_config(_make_config(state))
                await websocket.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "designing",
                        "description": description,
                        "revision": state.revision + 1,
                        "seed": seed,
                    }
                )
                try:
                    artifacts = await build_artifacts(
                        description,
                        feedback_question,
                        seed,
                    )
                except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                    state.design_in_progress = False
                    state.preview_armed = True
                    await session_input_handle.send_config(_make_config(state))
                    await websocket.send_json(
                        {
                            "type": "voice_design_status",
                            "status": "error",
                            "message": str(exc),
                        }
                    )
                    return "error", exc
                except Exception as exc:
                    logger.exception("Unexpected revision prefetch failure")
                    state.design_in_progress = False
                    state.preview_armed = True
                    await session_input_handle.send_config(_make_config(state))
                    await websocket.send_json(
                        {
                            "type": "voice_design_status",
                            "status": "error",
                            "message": str(exc),
                        }
                    )
                    return "error", exc

                if (
                    state.prefetch_task is not this_task
                    or state.prefetch_turn_sequence != state.turn_sequence
                ):
                    prefetched_voice_id, _ = artifacts
                    await designer.delete_voice(prefetched_voice_id)
                    return "stale", None

                new_voice_id, feedback_audio = artifacts
                await activate_voice(
                    description=description,
                    seed=seed,
                    new_voice_id=new_voice_id,
                    feedback_question=feedback_question,
                    feedback_audio=feedback_audio,
                    input_handle=session_input_handle,
                    ws=watched_socket,
                )
                return "active", new_voice_id
            except asyncio.CancelledError:
                if artifacts is not None:
                    prefetched_voice_id, _ = artifacts
                    # Activation commits the promoted voice before feedback
                    # playback finishes. Never delete an ID now referenced by
                    # session state merely because playback was interrupted.
                    if state.draft_voice_id != prefetched_voice_id:
                        await designer.delete_voice(prefetched_voice_id)
                raise
            finally:
                # Never leave an already-finished task pinned in session state.
                # The claiming callback keeps its own task/result references.
                if state.prefetch_task is this_task:
                    restore_lifecycle = state.design_in_progress
                    clear_prefetch(this_task)
                    if restore_lifecycle:
                        state.design_in_progress = False
                        state.preview_armed = True
                        try:
                            await session_input_handle.send_config(_make_config(state))
                        except Exception:
                            logger.exception(
                                "Could not restore config after prefetch cancellation"
                            )
                        if state.draft_voice_id:
                            try:
                                await websocket.send_json(
                                    {
                                        "type": "voice_design_status",
                                        "status": "active",
                                        "voice_id": state.draft_voice_id,
                                        "description": state.description,
                                        "revision": state.revision,
                                        "seed": state.seed,
                                    }
                                )
                            except Exception:
                                logger.debug(
                                    "Socket closed while restoring voice design status"
                                )

        task = asyncio.create_task(run_prefetch())
        state.prefetch_task = task

    def classify_turn_route(user_text: str) -> tuple[str | None, str | None]:
        if len(re.findall(r"\w+", user_text, re.UNICODE)) < 2:
            return None, None
        requested_language = _requested_language_switch(user_text)
        if requested_language and requested_language != state.language:
            return "language", requested_language
        if _requested_voice_source(user_text):
            return "voice_source", None
        if state.description and (caption_edit.is_voice_edit_intent(user_text) or caption_edit.is_explicit_edit_request(
            state.description,
            user_text,
            state.language,
        )):
            return "revision", None
        if caption_edit.is_conversation_request(user_text):
            return "conversation", None
        return None, None

    async def observe_partial_user_text(user_text: str) -> None:
        state.latest_user_text = user_text
        route, pending_language = classify_turn_route(user_text)
        if (
            route == state.turn_route
            and pending_language == state.pending_language
        ):
            return
        state.turn_route = route
        state.pending_language = pending_language
        if (
            session_input_handle is not None
            and not state.design_in_progress
            and not state.language_switch_in_progress
            and not state.direct_response_in_progress
        ):
            await session_input_handle.send_config(_make_config(state))
            state.language_route_armed = route == "language"

    async def observe_user_turn(user_text: str) -> None:
        state.turn_sequence += 1
        state.latest_user_text = user_text
        state.language_tool_handled = False
        if state.prefetch_task is not None:
            await cancel_prefetch("a newer user turn superseded the revision")
        state.turn_route, state.pending_language = classify_turn_route(user_text)
        requested_language = _requested_language_switch(user_text)
        if (
            requested_language
            and requested_language != state.language
            and session_input_handle is not None
        ):
            if (
                state.language_route_armed
                and state.pending_language == requested_language
            ):
                # The config was installed during streaming ASR, before Gradbot
                # snapshots the LLM request. Its forced switch_language call will
                # provide the terminal result that reliably triggers reset_asr.
                return
            # PhoneLLM occasionally acknowledges a language command without
            # emitting its app tool. Apply unambiguous commands at the EOT
            # boundary so the following model turn only performs reset_asr.
            await cancel_prefetch("explicit language switch")
            state.language_tool_handled = True
            state.language = requested_language
            state.latest_user_text = ""
            state.turn_route = None
            state.pending_language = None
            state.language_switch_in_progress = True
            state.language_route_armed = False
            state.preview_armed = False
            watched_socket.suppress_until_cancelled()
            await session_input_handle.send_config(_make_config(state))
            await websocket.send_json(
                {
                    "type": "language_changed",
                    "language": requested_language,
                    "language_name": LANGUAGE_NAMES[requested_language],
                }
            )
            asyncio.create_task(
                play_direct_confirmation(
                    text=LANGUAGE_SWITCH_CONFIRMATIONS[requested_language],
                    input_handle=session_input_handle,
                    ws=watched_socket,
                )
            )
            return
        requested_source = _requested_voice_source(user_text)
        if requested_source and session_input_handle is not None:
            if requested_source == "designed" and not state.draft_voice_id:
                requested_source = None
            else:
                await cancel_prefetch("explicit conversation voice switch")
                if requested_source == "agent":
                    state.voice_id = state.agent_voice_id
                    confirmation = "You're hearing my original voice again."
                else:
                    state.voice_id = state.draft_voice_id
                    confirmation = "You're hearing the latest designed voice again."
                state.latest_user_text = ""
                state.direct_response_in_progress = True
                state.preview_armed = False
                watched_socket.suppress_until_cancelled()
                await session_input_handle.send_config(_make_config(state))
                asyncio.create_task(
                    play_direct_confirmation(
                        text=confirmation,
                        input_handle=session_input_handle,
                        ws=watched_socket,
                    )
                )
                return
        if not (
            state.design_in_progress
            or state.language_switch_in_progress
            or state.direct_response_in_progress
        ):
            state.preview_armed = True
        start_revision_prefetch(user_text)

    async def observe_llm_started() -> None:
        if not state.language_switch_in_progress or session_input_handle is None:
            return
        # The reset generation has already snapshotted its forced reset_asr
        # configuration when this event arrives. Switch subsequent tool-result
        # continuations to sterile direct-response mode so the private reset is
        # exactly once rather than a forced tool loop.
        state.language_switch_in_progress = False
        state.direct_response_in_progress = True
        await session_input_handle.send_config(_make_config(state))

    watched_socket.on_user_text = observe_partial_user_text
    watched_socket.on_user_turn = observe_user_turn
    watched_socket.on_llm_started = observe_llm_started

    def on_start(msg: dict) -> gradbot.SessionConfig:
        state.agent_voice_id = msg.get("voice_id") or DEFAULT_VOICE_ID
        state.voice_id = state.agent_voice_id
        language = msg.get("language") or DEFAULT_LANGUAGE
        state.language = language if language in gradbot.LANGUAGES else DEFAULT_LANGUAGE
        try:
            state.speed = max(0.5, min(2.0, float(msg.get("speed", 1.0))))
        except (TypeError, ValueError):
            state.speed = 1.0
        return _make_config(state, speaks_first=True)

    def on_config(msg: dict) -> gradbot.SessionConfig:
        # The browser echoes the first STT text chunk of each real user turn.
        # Tool-result continuations do not emit this signal, so they cannot arm
        # another Voice Design API request.
        if msg.get("user_activity") and "user_text" not in msg:
            state.turn_route = None
            state.pending_language = None
            state.language_route_armed = False
            if not (
                state.design_in_progress
                or state.language_switch_in_progress
                or state.direct_response_in_progress
            ):
                # A late tool callback from the previous turn must not borrow the
                # previous transcript after the user starts speaking again.
                state.latest_user_text = ""
                state.preview_armed = True
            # The browser reports activity after receiving its first ASR chunk.
            # Reclassify that already-buffered text so this config update does
            # not erase an early ordinary-question or language route.
            partial_text = watched_socket.current_user_text
            if partial_text:
                state.latest_user_text = partial_text
                state.turn_route, state.pending_language = classify_turn_route(
                    partial_text
                )
                state.language_route_armed = state.turn_route == "language"
        if "user_text" in msg:
            state.latest_user_text = str(msg.get("user_text") or "").strip()
            state.turn_route, state.pending_language = classify_turn_route(
                state.latest_user_text
            )
            start_revision_prefetch(state.latest_user_text)
        if "speed" in msg:
            try:
                state.speed = max(0.5, min(2.0, float(msg["speed"])))
            except (TypeError, ValueError):
                pass
        return _make_config(state)

    async def on_tool_call(handle, input_handle, ws) -> None:
        nonlocal session_input_handle
        session_input_handle = input_handle

        if state.language_switch_in_progress or state.direct_response_in_progress:
            logger.warning("Ignoring %s during a direct response", handle.name)
            await settle_ignored_tool(handle, input_handle, "a reply is already playing")
            return

        if state.prefetch_task is not None and (
            state.prefetch_turn_sequence != state.turn_sequence
            or " ".join(state.prefetch_user_text.casefold().split())
            != " ".join(state.latest_user_text.casefold().split())
        ):
            await cancel_prefetch("the prefetch belongs to an earlier user turn")

        if state.prefetch_task is not None and handle.name != "preview_voice":
            await cancel_prefetch(
                f"PhoneLLM selected {handle.name} instead of preview_voice"
            )

        if handle.name == "preview_voice":
            requested_changes = str(
                handle.args.get("voice_description") or ""
            ).strip()
            if state.prefetch_task is None and (
                not state.latest_user_text
                or caption_edit.is_conversation_request(state.latest_user_text)
            ):
                logger.warning(
                    "Ignoring preview_voice without a current voice-design request"
                )
                await settle_ignored_tool(
                    handle,
                    input_handle,
                    "the latest utterance was ordinary conversation",
                )
                return
            watched_socket.suppress_on_next_llm_start()
            tool_resolved = False

            async def resolve_preview(payload: dict) -> None:
                nonlocal tool_resolved
                if tool_resolved:
                    return
                watched_socket.suppress_until_tts_end()
                await handle.send_json(payload)
                tool_resolved = True

            async def fail_preview(exc: BaseException) -> None:
                state.design_in_progress = False
                state.preview_armed = False
                clear_prefetch()
                await input_handle.send_config(_make_config(state))
                await ws.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "error",
                        "message": str(exc),
                    }
                )
                if not tool_resolved:
                    await handle.send_error(str(exc))
                    tool_resolved = True

            async def complete_preview(
                description: str,
                seed: int,
                feedback_question: str,
                artifacts: tuple[str, bytes],
            ) -> None:
                new_voice_id, feedback_audio = artifacts
                await activate_voice(
                    description=description,
                    seed=seed,
                    new_voice_id=new_voice_id,
                    feedback_question=feedback_question,
                    feedback_audio=feedback_audio,
                    input_handle=input_handle,
                    ws=ws,
                )
                await resolve_preview(
                    {
                        "success": True,
                        "status": "active",
                        "voice_id": state.draft_voice_id,
                        "revision": state.revision,
                        "feedback_spoken": True,
                        "message": (
                            "The new voice is active and already asked for feedback. "
                            "Return an empty response and wait for the user."
                        ),
                    }
                )

            async def report_too_subtle() -> None:
                if not state.draft_voice_id:
                    await fail_preview(
                        voice_generator.VoiceDesignError(
                            "Gradium returned an existing voice for the first draft"
                        )
                    )
                    return
                too_subtle = TOO_SUBTLE_QUESTIONS.get(
                    state.language,
                    TOO_SUBTLE_QUESTIONS["en"],
                )
                state.design_in_progress = False
                state.preview_armed = False
                await input_handle.send_config(_make_config(state))
                try:
                    feedback_audio = await designer.render_preview(
                        state.draft_voice_id,
                        too_subtle,
                    )
                except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                    await fail_preview(exc)
                    return
                await ws.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "active",
                        "voice_id": state.draft_voice_id,
                        "description": state.description,
                        "revision": state.revision,
                        "seed": state.seed,
                        "too_subtle": True,
                    }
                )
                await send_feedback_audio(
                    ws,
                    text=too_subtle,
                    audio=feedback_audio,
                    revision=state.revision,
                )
                await resolve_preview(
                    {
                        "success": True,
                        "status": "active",
                        "voice_id": state.draft_voice_id,
                        "revision": state.revision,
                        "feedback_spoken": True,
                        "too_subtle": True,
                        "message": (
                            "The existing voice asked what to push further. Return "
                            "an empty response and wait for the user."
                        ),
                    }
                )

            prefetch_task = state.prefetch_task
            if prefetch_task is not None and state.prefetch_claim is not None:
                if state.prefetch_claimed:
                    logger.warning("Ignoring duplicate claim for revision prefetch")
                    await settle_ignored_tool(
                        handle,
                        input_handle,
                        "this revision is already being built",
                    )
                    return
                # Tool callbacks can run concurrently. Claim synchronously before
                # the first await so only one callback can activate the artifacts.
                state.prefetch_claimed = True
                await input_handle.send_config(_make_config(state))
                state.prefetch_claim.set()
                try:
                    outcome, payload = await prefetch_task
                except asyncio.CancelledError:
                    clear_prefetch(prefetch_task)
                    await settle_ignored_tool(
                        handle,
                        input_handle,
                        "the user superseded this revision",
                    )
                    return
                except Exception as exc:
                    logger.exception("Claimed revision prefetch failed")
                    await fail_preview(exc)
                    return
                clear_prefetch(prefetch_task)
                if outcome == "active":
                    await resolve_preview(
                        {
                            "success": True,
                            "status": "active",
                            "voice_id": payload,
                            "revision": state.revision,
                            "feedback_spoken": True,
                            "message": (
                                "The new voice is active and already asked for "
                                "feedback. Return an empty response and wait."
                            ),
                        }
                    )
                elif outcome == "error":
                    await handle.send_error(str(payload))
                    tool_resolved = True
                elif outcome == "stale":
                    await settle_ignored_tool(
                        handle, input_handle, "this revision was superseded"
                    )
                return

            if state.design_in_progress:
                logger.warning(
                    "Ignoring duplicate preview_voice while a design is in progress"
                )
                await settle_ignored_tool(
                    handle,
                    input_handle,
                    "this revision is already being built",
                )
                return
            if not state.preview_armed:
                logger.warning("Rejecting unarmed preview_voice tool call")
                await settle_ignored_tool(
                    handle,
                    input_handle,
                    "this request was already handled",
                )
                return
            try:
                description = _resolve_voice_description(
                    state.description,
                    requested_changes,
                    state.latest_user_text,
                    state.language,
                )
            except voice_generator.VoiceDesignError as exc:
                await handle.send_error(str(exc))
                return

            if state.draft_voice_id and description == state.description:
                feedback_question = _feedback_question(
                    state.language,
                    state.revision - 1,
                )
                logger.warning("Reusing completed duplicate preview_voice request")
                state.preview_armed = False
                await input_handle.send_config(_make_config(state))
                try:
                    feedback_audio = await designer.render_preview(
                        state.draft_voice_id,
                        feedback_question,
                    )
                except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                    await fail_preview(exc)
                    return
                await ws.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "active",
                        "voice_id": state.draft_voice_id,
                        "description": state.description,
                        "revision": state.revision,
                        "seed": state.seed,
                    }
                )
                await send_feedback_audio(
                    ws,
                    text=feedback_question,
                    audio=feedback_audio,
                    revision=state.revision,
                )
                await resolve_preview(
                    {
                        "success": True,
                        "status": "active",
                        "voice_id": state.draft_voice_id,
                        "revision": state.revision,
                        "feedback_spoken": True,
                        "message": (
                            "The active voice already asked for feedback. Return an "
                            "empty response and wait for the user."
                        ),
                    }
                )
                return

            state.preview_armed = False
            state.design_in_progress = True
            await input_handle.send_config(_make_config(state))

            build_turn_sequence = state.turn_sequence
            seed = state.seed if state.seed is not None else random.randint(
                0, voice_generator.MAX_SEED
            )
            feedback_question = _feedback_question(state.language, state.revision)
            await ws.send_json(
                {
                    "type": "voice_design_status",
                    "status": "designing",
                    "description": description,
                    "revision": state.revision + 1,
                    "seed": seed,
                }
            )
            try:
                artifacts = await build_artifacts(
                    description,
                    feedback_question,
                    seed,
                )
            except voice_generator.DuplicateVoiceError:
                await report_too_subtle()
                return
            except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                await fail_preview(exc)
                return

            # A user may correct the request while the API is polling. Never let
            # artifacts from the older turn become the live voice. For an
            # explicit correction, fold the new request into the description and
            # build the latest turn; otherwise cancel cleanly and re-arm tools.
            while build_turn_sequence != state.turn_sequence:
                latest_request = state.latest_user_text.strip()
                new_voice_id, _ = artifacts
                await designer.delete_voice(new_voice_id)
                if not caption_edit.is_explicit_edit_request(
                    description,
                    latest_request,
                    state.language,
                ):
                    state.design_in_progress = False
                    state.preview_armed = True
                    await input_handle.send_config(_make_config(state))
                    await ws.send_json(
                        {
                            "type": "voice_design_status",
                            "status": "cancelled",
                            "description": state.description,
                            "revision": state.revision,
                        }
                    )
                    await resolve_preview(
                        {
                            "success": True,
                            "status": "superseded",
                            "message": (
                                "The user moved on before this draft finished. "
                                "Return an empty response and wait for the user."
                            ),
                        }
                    )
                    return

                description = _append_voice_changes(description, latest_request)
                build_turn_sequence = state.turn_sequence
                feedback_question = _feedback_question(
                    state.language,
                    state.revision,
                )
                await ws.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "designing",
                        "description": description,
                        "revision": state.revision + 1,
                        "seed": seed,
                    }
                )
                try:
                    artifacts = await build_artifacts(
                        description,
                        feedback_question,
                        seed,
                    )
                except voice_generator.DuplicateVoiceError:
                    await report_too_subtle()
                    return
                except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                    await fail_preview(exc)
                    return

            await complete_preview(
                description,
                seed,
                feedback_question,
                artifacts,
            )
            return

        if handle.name == "finalize_voice":
            if not state.draft_voice_id or not state.description:
                await handle.send_error("Create at least one custom voice first")
                return
            voice_name = str(handle.args.get("voice_name") or "").strip()
            if not voice_name:
                timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                voice_name = f"Voice Workshop {timestamp}"
            state.direct_response_in_progress = True
            state.preview_armed = False
            watched_socket.suppress_until_cancelled()
            try:
                await designer.update_voice(
                    state.draft_voice_id,
                    name=voice_name,
                    description=state.description,
                )
            except (httpx.HTTPError, voice_generator.VoiceDesignError) as exc:
                state.direct_response_in_progress = False
                state.preview_armed = True
                watched_socket.cancel_output_suppression()
                await input_handle.send_config(_make_config(state))
                await ws.send_json(
                    {
                        "type": "voice_design_status",
                        "status": "error",
                        "message": str(exc),
                    }
                )
                await handle.send_error(str(exc))
                return
            state.voice_id = state.draft_voice_id
            state.saved_voice_ids.add(state.draft_voice_id)
            state.finalized = True
            await input_handle.send_config(_make_config(state))
            await ws.send_json(
                {
                    "type": "voice_design_status",
                    "status": "finalized",
                    "voice_id": state.draft_voice_id,
                    "description": state.description,
                    "revision": state.revision,
                }
            )
            await handle.send_json(
                {
                    "success": True,
                    "voice_id": state.draft_voice_id,
                    "message": (
                        "The voice has been saved and the application is playing "
                        "the confirmation. Return an empty response."
                    ),
                }
            )
            await play_direct_confirmation(
                text="Perfect, I'll keep this voice.",
                input_handle=input_handle,
                ws=ws,
            )
            return

        if handle.name == "switch_conversation_voice":
            target = str(handle.args.get("target") or "").strip().lower()
            if target == "agent":
                state.voice_id = state.agent_voice_id
                confirmation = "You're hearing my original voice again."
            elif target == "designed" and state.draft_voice_id:
                state.voice_id = state.draft_voice_id
                confirmation = "You're hearing the latest designed voice again."
            else:
                await handle.send_error("There is no designed voice to switch to yet")
                return
            state.direct_response_in_progress = True
            state.preview_armed = False
            watched_socket.suppress_until_cancelled()
            await input_handle.send_config(_make_config(state))
            await handle.send_json(
                {
                    "success": True,
                    "voice_id": state.voice_id,
                    "confirmation_spoken": True,
                    "message": (
                        "The application is playing the confirmation directly. "
                        "Return an empty response."
                    ),
                }
            )
            await play_direct_confirmation(
                text=confirmation,
                input_handle=input_handle,
                ws=ws,
            )
            return

        if handle.name == "switch_language":
            state.language_route_armed = False
            if state.language_tool_handled:
                logger.warning("Ignoring duplicate switch_language tool call")
                await settle_ignored_tool(
                    handle,
                    input_handle,
                    "this language request was already handled",
                )
                return
            requested_language = str(handle.args.get("language") or "").strip().lower()
            language = LANGUAGE_ALIASES.get(requested_language)
            if language not in gradbot.LANGUAGES:
                await handle.send_error(
                    "Supported languages are English, French, Spanish, German, "
                    "and Portuguese"
                )
                return

            user_text = state.latest_user_text
            if not _is_explicit_language_switch(user_text, language):
                state.language_tool_handled = True
                logger.warning(
                    "Rejecting language switch without explicit user intent: %r",
                    user_text,
                )
                await handle.send_json(
                    {
                        "success": True,
                        "status": "not_a_language_switch",
                        "language": state.language,
                        "message": (
                            "Do not change the conversation language. The user's "
                            f"latest request was: {user_text!r}. A language-named "
                            "voice or accent is a voice-design trait. Call "
                            "preview_voice now with the requested voice traits, "
                            "without asking another question."
                        ),
                    }
                )
                return

            if language == state.language:
                state.language_tool_handled = True
                state.latest_user_text = ""
                await handle.send_json(
                    {
                        "success": True,
                        "language": language,
                        "language_name": LANGUAGE_NAMES[language],
                        "already_active": True,
                        "message": (
                            "This language is already active. Do not call reset_asr "
                            "or any other tool. Continue the conversation normally."
                        ),
                    }
                )
                return

            state.language_tool_handled = True
            state.language = language
            state.latest_user_text = ""
            state.turn_route = None
            state.pending_language = None
            state.language_switch_in_progress = True
            state.preview_armed = False
            watched_socket.suppress_until_cancelled()
            await input_handle.send_config(_make_config(state))
            await ws.send_json(
                {
                    "type": "language_changed",
                    "language": language,
                    "language_name": LANGUAGE_NAMES[language],
                }
            )
            await handle.send_json(
                {
                    "success": True,
                    "language": language,
                    "language_name": LANGUAGE_NAMES[language],
                    "confirmation": LANGUAGE_SWITCH_CONFIRMATIONS[language],
                    "message": (
                        "The confirmation is played directly by the application. "
                        "Say nothing. Call reset_asr exactly once, then return an "
                        "empty response."
                    ),
                }
            )
            await play_direct_confirmation(
                text=LANGUAGE_SWITCH_CONFIRMATIONS[language],
                input_handle=input_handle,
                ws=ws,
            )
            return

        await handle.send_error(f"Unknown tool: {handle.name}")

    try:
        await gradbot.websocket.handle_session(
            watched_socket,
            config=cfg,
            on_start=on_start,
            on_config=on_config,
            on_tool_call=on_tool_call,
        )
    finally:
        if state.prefetch_task is not None:
            state.prefetch_task.cancel()
            await asyncio.gather(state.prefetch_task, return_exceptions=True)
            clear_prefetch()
        await flush_retired_voices()
        if (
            state.draft_voice_id
            and state.draft_voice_id not in state.saved_voice_ids
        ):
            await designer.delete_voice(state.draft_voice_id)
        await designer.aclose()


gradbot.routes.setup(
    app,
    config=cfg,
    static_dir=APP_DIR / "static",
    with_voices=True,
)
