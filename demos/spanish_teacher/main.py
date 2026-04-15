"""
Spanish Teacher Demo - Language learning with voice

A FastAPI backend that exposes:
- WebSocket /ws/chat - real-time voice conversation with a Spanish teacher

The AI teaches Spanish sentences, explains meanings word by word,
and tracks the user's progress through tool calls.

Run with: uv run uvicorn main:app --reload
"""

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket

import gradbot

gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))


_cfg = gradbot.config.load(Path(__file__).parent)



# Spanish sentences to practice (beginner-friendly)
SENTENCES = [
    {
        "spanish": "Buenos dias, como estas?",
        "english": "Good morning, how are you?",
        "words": [
            ("Buenos", "Good (masculine plural)"),
            ("dias", "days/morning"),
            ("como", "how"),
            ("estas", "are you (informal)"),
        ]
    },
    {
        "spanish": "Me llamo Maria y soy de Mexico.",
        "english": "My name is Maria and I am from Mexico.",
        "words": [
            ("Me", "Myself"),
            ("llamo", "I call (reflexive: I am called)"),
            ("Maria", "Maria (name)"),
            ("y", "and"),
            ("soy", "I am"),
            ("de", "from"),
            ("Mexico", "Mexico"),
        ]
    },
    {
        "spanish": "Donde esta la biblioteca?",
        "english": "Where is the library?",
        "words": [
            ("Donde", "Where"),
            ("esta", "is (location)"),
            ("la", "the (feminine)"),
            ("biblioteca", "library"),
        ]
    },
    {
        "spanish": "Quiero un cafe con leche, por favor.",
        "english": "I want a coffee with milk, please.",
        "words": [
            ("Quiero", "I want"),
            ("un", "a/one"),
            ("cafe", "coffee"),
            ("con", "with"),
            ("leche", "milk"),
            ("por favor", "please"),
        ]
    },
    {
        "spanish": "Hace mucho calor hoy.",
        "english": "It is very hot today.",
        "words": [
            ("Hace", "It makes (weather expression)"),
            ("mucho", "much/very"),
            ("calor", "heat/hot"),
            ("hoy", "today"),
        ]
    },
    {
        "spanish": "Tengo hambre, vamos a comer.",
        "english": "I am hungry, let's go eat.",
        "words": [
            ("Tengo", "I have"),
            ("hambre", "hunger (I have hunger = I am hungry)"),
            ("vamos", "let's go / we go"),
            ("a", "to"),
            ("comer", "to eat"),
        ]
    },
]


def build_tools() -> list[gradbot.ToolDef]:
    """Build tool definitions for the Spanish teacher."""
    return [
        gradbot.ToolDef(
            name="get_next_sentence",
            description="Get the next Spanish sentence to teach. Call this when the student has successfully repeated the current sentence or when starting the lesson.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": [],
            }),
        ),
        gradbot.ToolDef(
            name="record_success",
            description="Record that the student successfully repeated the sentence. Call this when the student's pronunciation was close enough to the target.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": "Brief positive feedback for the student"
                    }
                },
                "required": ["feedback"],
            }),
        ),
        gradbot.ToolDef(
            name="record_failure",
            description="Record that the student needs more practice. Call this when the pronunciation was too far from the target after an attempt.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": "Encouraging feedback with tips for improvement"
                    }
                },
                "required": ["feedback"],
            }),
        ),
    ]


_PROMPTS_DIR = Path(__file__).parent / "prompts"


def get_system_prompt() -> str:
    """Build the system prompt for the Spanish teacher."""
    return (_PROMPTS_DIR / "system.txt").read_text()


class SessionState:
    def __init__(self):
        self.current_sentence_idx = -1  # Start at -1, first get_next_sentence will go to 0
        self.successes = 0
        self.failures = 0

    def next_sentence(self):
        self.current_sentence_idx += 1
        if self.current_sentence_idx >= len(SENTENCES):
            return None  # Lesson complete
        return SENTENCES[self.current_sentence_idx]

    def current_sentence(self):
        if 0 <= self.current_sentence_idx < len(SENTENCES):
            return SENTENCES[self.current_sentence_idx]
        return None


app = FastAPI(title="Spanish Teacher Demo")

logger = logging.getLogger(__name__)


def make_session_config() -> gradbot.SessionConfig:
    """Create the session config for Valentina."""
    tools = build_tools()
    return gradbot.SessionConfig(
        voice_id="B36pbz5_UoWn4BDl",  # Valentina
        instructions=get_system_prompt(),
        language=gradbot.Lang.Es,
        tools=tools,
        **{
            "flush_duration_s": FLUSH_FOR_S,
            "rewrite_rules": gradbot.Lang.Es.rewrite_rules,
            "assistant_speaks_first": True,
        } | _cfg.session_kwargs,
    )


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    state = SessionState()

    async def handle_tool_call(handle, input_handle, websocket):
        """Handle teacher tool calls."""
        tool_name = handle.name
        args = handle.args
        logger.info("Tool call received: %s", tool_name)

        if tool_name == "get_next_sentence":
            sentence = state.next_sentence()
            if sentence is None:
                # Lesson complete!
                await handle.send(json.dumps({
                    "lesson_complete": True,
                    "message": "Congratulations! You have completed all the sentences!",
                    "total_successes": state.successes,
                    "total_failures": state.failures,
                }))
                await websocket.send_json({
                    "type": "lesson_complete",
                    "successes": state.successes,
                    "failures": state.failures,
                })
            else:
                # Send sentence info to LLM
                word_explanations = [f"{w}: {meaning}" for w, meaning in sentence["words"]]
                await handle.send(json.dumps({
                    "spanish": sentence["spanish"],
                    "english": sentence["english"],
                    "words": word_explanations,
                    "sentence_number": state.current_sentence_idx + 1,
                    "total_sentences": len(SENTENCES),
                }))
                # Notify client
                await websocket.send_json({
                    "type": "new_sentence",
                    "spanish": sentence["spanish"],
                    "english": sentence["english"],
                    "sentence_number": state.current_sentence_idx + 1,
                    "total_sentences": len(SENTENCES),
                })

        elif tool_name == "record_success":
            state.successes += 1
            feedback = args.get("feedback", "Great job!")
            await handle.send(json.dumps({
                "recorded": True,
                "total_successes": state.successes,
            }))
            await websocket.send_json({
                "type": "score_update",
                "successes": state.successes,
                "failures": state.failures,
            })

        elif tool_name == "record_failure":
            state.failures += 1
            feedback = args.get("feedback", "Keep trying!")
            await handle.send(json.dumps({
                "recorded": True,
                "total_failures": state.failures,
            }))
            await websocket.send_json({
                "type": "score_update",
                "successes": state.successes,
                "failures": state.failures,
            })

        else:
            await handle.send_error(f"Unknown tool: {tool_name}")

    await gradbot.websocket.handle_session(
        websocket,
        on_start=lambda msg: make_session_config(),
        on_tool_call=handle_tool_call,
        run_kwargs=_cfg.client_kwargs,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


gradbot.routes.setup(
    app,
    config=_cfg,
    static_dir=Path(__file__).parent / "static",
)

