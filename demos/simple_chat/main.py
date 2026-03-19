"""Simple voice chat demo. Run with: uvicorn main:app --reload"""

import os
from pathlib import Path

from fastapi import FastAPI, WebSocket

import gradbot
from gradbot.demo_config import load_config, session_config_overrides, merge_overrides, client_config
from gradbot.fastapi import websocket_chat_handler, setup_demo_routes

gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

app = FastAPI(title="Gradbot Demo")


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


setup_demo_routes(app, static_dir=Path(__file__).parent / "static", use_pcm=USE_PCM, voices=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
