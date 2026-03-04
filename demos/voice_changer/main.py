"""
Voice Changer Demo - AI can switch between voices

A FastAPI backend that exposes:
- GET /api/voices - list available flagship voices with descriptions
- WebSocket /ws/chat - real-time voice conversation with voice switching

The AI is given tools to switch between voices and is prompted to have
conversations and occasionally ask if the user wants to talk to someone else.

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

import pygradbot

# Initialize Rust logging (outputs to stderr)
pygradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)


def lang_to_code(lang: pygradbot.Lang) -> str:
    """Convert Lang enum to language code."""
    if lang == pygradbot.Lang.En:
        return "en"
    elif lang == pygradbot.Lang.Fr:
        return "fr"
    elif lang == pygradbot.Lang.De:
        return "de"
    elif lang == pygradbot.Lang.Es:
        return "es"
    elif lang == pygradbot.Lang.Pt:
        return "pt"
    return "en"


def build_voice_tools() -> list[pygradbot.ToolDef]:
    """Build tool definitions for each voice."""
    tools = []
    for voice in pygradbot.flagship_voices():
        # Create a tool for switching to this voice
        tool = pygradbot.ToolDef(
            name=f"switch_to_{voice.name.lower()}",
            description=f"Switch to {voice.name}'s voice. {voice.description}",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": [],
            }),
        )
        tools.append(tool)
    return tools


def get_system_prompt(current_voice_name: str) -> str:
    """Build the system prompt for the voice changer demo."""
    voice = pygradbot.flagship_voice(current_voice_name)

    return f"""You are {voice.name}, a friendly AI assistant participating in a voice demo.
Your current voice is {voice.name} ({voice.gender}, from {voice.country}).

{voice.description}

You have access to many tools named switch_to_* that let you change your voice to different personas. Check the tool descriptions to see what voices are available.

This is a demo of real-time voice switching. Your behavior:
1. Have a brief friendly conversation (2-3 exchanges max)
2. Then ask "Would you like to talk to someone else?" and suggest a different voice
3. When the user agrees, CALL THE APPROPRIATE TOOL to switch voices
4. After switching, introduce yourself as the new character
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Voice Changer Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Voice Changer Demo", lifespan=lifespan)


@app.get("/api/voices")
async def list_voices():
    """Return list of available flagship voices with full details."""
    voices = [
        {
            "name": v.name,
            "voice_id": v.voice_id,
            "language": lang_to_code(v.language),
            "country": v.country.code(),
            "country_name": str(v.country),
            "gender": str(v.gender),
            "description": v.description,
        }
        for v in pygradbot.flagship_voices()
    ]
    return JSONResponse(content={"voices": voices})


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for voice chat with voice switching.

    Protocol:
    - Client sends JSON: {"type": "start", "voice_name": "Emma"}
    - Client sends binary: raw audio data (PCM 16-bit, 24kHz, mono)
    - Server sends JSON: {"type": "transcript", "text": "...", "is_user": true/false}
    - Server sends JSON: {"type": "voice_change", "voice_name": "..."}
    - Server sends JSON: {"type": "event", "event": "..."}
    - Server sends binary: audio data (PCM)
    - Client sends JSON: {"type": "stop"} to end
    """
    await websocket.accept()

    try:
        # Wait for start message
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        voice_name = start_msg.get("voice_name", "Emma")

        # Validate voice
        try:
            voice = pygradbot.flagship_voice(voice_name)
        except RuntimeError:
            await websocket.close(code=4001, reason=f"Unknown voice: {voice_name}")
            return

        current_voice = voice_name
        print(f"Starting chat with voice={voice_name}")

        # Build tools for voice switching
        tools = build_voice_tools()
        print(f"Built {len(tools)} voice-switching tools:")
        for t in tools:
            print(f"  - {t.name}")

        # Create session config
        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(voice_name),
            language=voice.language,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=voice.language.rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

        # Create clients and start session
        input_handle, output_handle = await pygradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.Pcm if USE_PCM else pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_tool_call(tool_call, tool_handle):
            """Handle voice switching tool calls."""
            nonlocal current_voice

            tool_name = tool_call.tool_name
            print(f"Tool call: {tool_name}")

            # Check if this is a voice switch
            if tool_name.startswith("switch_to_"):
                new_voice_name = tool_name[len("switch_to_"):].capitalize()
                # Handle multi-word names
                for v in pygradbot.flagship_voices():
                    if v.name.lower() == tool_name[len("switch_to_"):]:
                        new_voice_name = v.name
                        break

                try:
                    new_voice = pygradbot.flagship_voice(new_voice_name)
                    current_voice = new_voice_name

                    # Update session config with new voice
                    new_config = pygradbot.SessionConfig(
                        voice_id=new_voice.voice_id,
                        instructions=get_system_prompt(new_voice_name),
                        language=new_voice.language,
                        tools=tools,
                        **merge_overrides(_OVERRIDES,
                            flush_duration_s=FLUSH_FOR_S,
                            rewrite_rules=new_voice.language.rewrite_rules,
                        ),
                    )
                    await input_handle.send_config(new_config)

                    # Notify client of voice change
                    await websocket.send_json({
                        "type": "voice_change",
                        "voice_name": new_voice_name,
                        "description": new_voice.description,
                    })

                    # Send success to LLM
                    await tool_handle.send(json.dumps({
                        "success": True,
                        "message": f"Voice switched to {new_voice_name}"
                    }))
                except RuntimeError as e:
                    await tool_handle.send_error(str(e))
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
                        # Send audio timing before binary data
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
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(handle_tool_call(msg.tool_call, msg.tool_call_handle))

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
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
