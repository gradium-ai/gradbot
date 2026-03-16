"""Voice Changer Demo - AI can switch between voices. Run with: uvicorn main:app --reload"""

import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import gradbot
from gradbot.fastapi import websocket_chat_handler

gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

app = FastAPI(title="Voice Changer Demo")


def lang_to_code(lang: gradbot.Lang) -> str:
    mapping = {
        gradbot.Lang.En: "en", gradbot.Lang.Fr: "fr", gradbot.Lang.De: "de",
        gradbot.Lang.Es: "es", gradbot.Lang.Pt: "pt",
    }
    return mapping.get(lang, "en")


VOICE_TOOLS = [
    gradbot.ToolDef(
        name=f"switch_to_{voice.name.lower()}",
        description=f"Switch to {voice.name}'s voice. {voice.description}",
        parameters_json=json.dumps({
            "type": "object",
            "properties": {},
            "required": [],
        }),
    )
    for voice in gradbot.flagship_voices()
]


def get_system_prompt(voice_name: str) -> str:
    voice = gradbot.flagship_voice(voice_name)
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


def make_session_config(voice_name: str) -> gradbot.SessionConfig:
    voice = gradbot.flagship_voice(voice_name)
    return gradbot.SessionConfig(
        voice_id=voice.voice_id,
        instructions=get_system_prompt(voice_name),
        language=voice.language,
        tools=VOICE_TOOLS,
        **merge_overrides(_OVERRIDES,
            flush_duration_s=FLUSH_FOR_S,
            rewrite_rules=voice.language.rewrite_rules,
            assistant_speaks_first=True,
        ),
    )


logger = logging.getLogger(__name__)


async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
    tool_name = tool_call.tool_name
    logger.info("Tool call received: %s", tool_name)

    if not tool_name.startswith("switch_to_"):
        await tool_handle.send_error(f"Unknown tool: {tool_name}")
        return

    # Resolve voice name from tool name
    raw_name = tool_name[len("switch_to_"):]
    new_voice_name = raw_name.capitalize()
    for v in gradbot.flagship_voices():
        if v.name.lower() == raw_name:
            new_voice_name = v.name
            break

    try:
        new_config = make_session_config(new_voice_name)
        await input_handle.send_config(new_config)

        new_voice = gradbot.flagship_voice(new_voice_name)
        await websocket.send_json({
            "type": "voice_change",
            "voice_name": new_voice_name,
            "description": new_voice.description,
        })

        await tool_handle.send(json.dumps({
            "success": True,
            "message": f"Voice switched to {new_voice_name}",
        }))
    except RuntimeError as e:
        await tool_handle.send_error(str(e))


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket_chat_handler(
        websocket,
        on_start=lambda msg: make_session_config(msg.get("voice_name", "Emma")),
        on_tool_call=handle_tool_call,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


_VOICES_RESPONSE = {"voices": [
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
]}


@app.get("/api/voices")
async def list_voices():
    return JSONResponse(content=_VOICES_RESPONSE)


@app.get("/api/audio-config")
async def audio_config():
    return JSONResponse(content={"pcm": USE_PCM})


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")


@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse(content={"error": "Frontend not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
