"""
Gradbot Demo - Voice AI Chat Application

A FastAPI backend that exposes:
- GET /api/voices - list available flagship voices
- WebSocket /ws/chat - real-time voice conversation

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


def lang_to_code(lang: gradbot.Lang) -> str:
    """Convert Lang enum to language code."""
    if lang == gradbot.Lang.En:
        return "en"
    elif lang == gradbot.Lang.Fr:
        return "fr"
    elif lang == gradbot.Lang.De:
        return "de"
    elif lang == gradbot.Lang.Es:
        return "es"
    elif lang == gradbot.Lang.Pt:
        return "pt"
    return "en"


def lang_to_name(lang: gradbot.Lang) -> str:
    """Convert Lang enum to language name."""
    if lang == gradbot.Lang.En:
        return "English"
    elif lang == gradbot.Lang.Fr:
        return "French"
    elif lang == gradbot.Lang.De:
        return "German"
    elif lang == gradbot.Lang.Es:
        return "Spanish"
    elif lang == gradbot.Lang.Pt:
        return "Portuguese"
    return "English"


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Gradbot Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Gradbot Demo", lifespan=lifespan)


@app.get("/api/voices")
async def list_voices():
    """Return list of available flagship voices."""
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
        for v in gradbot.flagship_voices()
    ]
    return JSONResponse(content={"voices": voices})


def make_session_config(voice_name: str, prompt: str) -> gradbot.SessionConfig:
    """Create a SessionConfig from voice name and prompt."""
    voice = gradbot.flagship_voice(voice_name)
    return gradbot.SessionConfig(
        voice_id=voice.voice_id,
        instructions=prompt,
        language=voice.language,
        **merge_overrides(_OVERRIDES,
            flush_duration_s=FLUSH_FOR_S,
            rewrite_rules=voice.language.rewrite_rules,
            assistant_speaks_first=True,
        ),
    )


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for voice chat.

    Protocol:
    - Client sends JSON: {"type": "start", "voice_name": "Emma", "prompt": "..."}
    - Client sends JSON: {"type": "config", "voice_name": "Leo", "prompt": "..."} to change mid-conversation
    - Client sends binary: raw audio data (PCM 16-bit, 24kHz, mono)
    - Server sends JSON: {"type": "transcript", "text": "...", "is_user": true/false}
    - Server sends JSON: {"type": "event", "event": "..."}
    - Server sends binary: audio data (Ogg Opus)
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
        prompt = start_msg.get("prompt", "I am a helpful assistant")

        # Look up the voice and create config
        try:
            config = make_session_config(voice_name, prompt)
        except RuntimeError:
            await websocket.close(code=4001, reason=f"Unknown voice: {voice_name}")
            return

        print(f"Starting chat with voice={voice_name}")

        # Create clients and start session
        # Uses GRADIUM_API_KEY, GRADIUM_BASE_URL, LLM_BASE_URL env vars
        input_handle, output_handle = await gradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=gradbot.AudioFormat.OggOpus,
            output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def process_output():
            """Receive output from gradbot and send to client."""
            print("process_output: starting")
            while not stop_event.is_set():
                try:
                    print("process_output: waiting for message...")
                    msg = await output_handle.receive()
                    print(f"process_output: got msg type={msg.msg_type if msg else None}")
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
                        # Send AI transcript with timing for text sync
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        # Send user transcript
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "event":
                        # Forward events to client
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    # Send error to client for UI display
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
            print("receive_audio: starting")
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        msg_type = data.get("type")
                        if msg_type == "stop":
                            print("receive_audio: got stop")
                            stop_event.set()
                            await input_handle.close()
                            break
                        elif msg_type == "config":
                            # Mid-conversation config change
                            new_voice = data.get("voice_name", "Emma")
                            new_prompt = data.get("prompt", "I am a helpful assistant")
                            print(f"receive_audio: config change voice={new_voice}")
                            try:
                                new_config = make_session_config(new_voice, new_prompt)
                                await input_handle.send_config(new_config)
                            except RuntimeError as e:
                                print(f"Config change error: {e}")
                                await websocket.send_json({
                                    "type": "error",
                                    "message": str(e) if DEBUG else "An error occurred while changing config",
                                })
                    elif "bytes" in msg:
                        print(f"receive_audio: got {len(msg['bytes'])} bytes")
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
