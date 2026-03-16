"""Simple voice chat demo. Run with: uvicorn main:app --reload"""

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

app = FastAPI(title="Gradbot Demo")


def lang_to_code(lang: gradbot.Lang) -> str:
    mapping = {
        gradbot.Lang.En: "en", gradbot.Lang.Fr: "fr", gradbot.Lang.De: "de",
        gradbot.Lang.Es: "es", gradbot.Lang.Pt: "pt",
    }
    return mapping.get(lang, "en")


def make_session_config(voice_name: str, prompt: str) -> gradbot.SessionConfig:
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


def _session_from_msg(msg: dict) -> gradbot.SessionConfig:
    return make_session_config(
        msg.get("voice_name", "Emma"),
        msg.get("prompt", "I am a helpful assistant"),
    )


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket_chat_handler(
        websocket,
        on_start=_session_from_msg,
        on_config=_session_from_msg,
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
