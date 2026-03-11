"""
Spanish Teacher Demo - Language learning with voice

A FastAPI backend that exposes:
- WebSocket /ws/chat - real-time voice conversation with a Spanish teacher

The AI teaches Spanish sentences, explains meanings word by word,
and tracks the user's progress through tool calls.

Run with: uvicorn main:app --reload
"""

import asyncio
import os
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import gradbot

# Initialize Rust logging (outputs to stderr)
gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

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


def get_system_prompt() -> str:
    """Build the system prompt for the Spanish teacher."""
    return """You are Valentina, a warm and patient Spanish teacher from Mexico City.

At the very start of the conversation, introduce yourself by saying:
"Hola! I am Valentina, I am from Mexico City, and I am your Spanish teacher today! Are you ready to learn some Spanish?"

Wait for the student to respond, then call get_next_sentence to get the first sentence to teach.

YOUR TEACHING METHOD:
1. When you receive a new sentence, first explain its overall meaning in English
2. Then go through EACH WORD one by one:
   - Say the Spanish word clearly
   - Explain what it means
   - Use it in context if helpful
3. Say the complete sentence slowly and clearly
4. Ask the student to repeat after you

HANDLING STUDENT RESPONSES:
- The speech recognition may not be perfect for Spanish spoken by beginners
- Be generous in accepting attempts - if it sounds roughly close, count it as success
- Focus on encouragement, not perfection
- If the student asks to repeat or says they didn't understand, repeat the explanation

IMPORTANT:
- Do NOT repeat back what the student said (the transcription is imperfect)
- Instead, respond based on how close their attempt sounded to the target
- If close enough, call record_success and then get_next_sentence
- If they need more practice, call record_failure and encourage them to try again
- Keep the energy positive and encouraging!

LANGUAGE RULE - VERY IMPORTANT:
- You MUST speak primarily in the student's language (English by default)
- Only say Spanish words when teaching them or when having the student repeat
- Do NOT switch to speaking full Spanish sentences outside of the lesson content
- Your explanations, encouragement, and conversation must stay in the student's language
- Example: Say "Great job! Now let's try the next word: 'Buenos' means 'good'" NOT "Muy bien! Ahora vamos con..."
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Spanish Teacher Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Spanish Teacher Demo", lifespan=lifespan)


# Session state
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


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for voice chat with the Spanish teacher.
    """
    await websocket.accept()

    try:
        # Wait for start message
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        print("Starting Spanish lesson...")

        # Session state
        state = SessionState()

        # Get Valentina's voice (Spanish, Mexican)
        voice = gradbot.flagship_voice("Valentina")

        # Build tools
        tools = build_tools()

        # Create session config
        config = gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(),
            language=voice.language,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=voice.language.rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

        # Create clients and start session
        input_handle, output_handle = await gradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=gradbot.AudioFormat.OggOpus,
            output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_tool_call(tool_call, tool_handle):
            """Handle teacher tool calls."""
            tool_name = tool_call.tool_name
            print(f"Tool call: {tool_name}")

            try:
                args = json.loads(tool_call.args_json) if tool_call.args_json else {}
            except json.JSONDecodeError:
                args = {}

            if tool_name == "get_next_sentence":
                sentence = state.next_sentence()
                if sentence is None:
                    # Lesson complete!
                    await tool_handle.send(json.dumps({
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
                    await tool_handle.send(json.dumps({
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
                await tool_handle.send(json.dumps({
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
                await tool_handle.send(json.dumps({
                    "recorded": True,
                    "total_failures": state.failures,
                }))
                await websocket.send_json({
                    "type": "score_update",
                    "successes": state.successes,
                    "failures": state.failures,
                })

            else:
                await tool_handle.send_error(f"Unknown tool: {tool_name}")

        async def process_output():
            """Receive output from gradbot and send to client."""
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        break

                    if msg.msg_type == "audio":
                        await websocket.send_json({
                            "type": "audio_timing",
                            "start_s": msg.start_s,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                            "interrupted": msg.interrupted,
                        })
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        # Send user transcript for debugging (STT may be imperfect)
                        print(f"[STT] {msg.text}")
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "tool_call":
                        await handle_tool_call(msg.tool_call, msg.tool_call_handle)

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    import traceback
                    traceback.print_exc()
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e) if DEBUG else "An error occurred during the session",
                        })
                    except:
                        pass
                    break

        async def receive_audio():
            """Receive audio from client and send to gradbot."""
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        msg_type = data.get("type")
                        if msg_type == "stop":
                            stop_event.set()
                            await input_handle.close()
                            break
                    elif "bytes" in msg:
                        await input_handle.send_audio(msg["bytes"])
                except WebSocketDisconnect:
                    stop_event.set()
                    await input_handle.close()
                    break
                except Exception as e:
                    print(f"Receive error: {e}")
                    stop_event.set()
                    break

        # Run both tasks
        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e) if DEBUG else "An error occurred while starting the session",
            })
        except:
            pass
    finally:
        try:
            await websocket.close()
        except:
            pass


@app.get("/api/audio-config")
async def audio_config():
    return JSONResponse(content={"pcm": USE_PCM})

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")


@app.get("/")
async def index():
    """Serve the main page."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse(
        content={"error": "Frontend not found. Place index.html in static/"},
        status_code=404
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
