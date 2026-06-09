"""Notre IA - a simple French Gradbot voice chat.

A French-speaking AI assistant answers questions out loud. Built on the same
shape as gradbot's simple_chat demo, talking to the hosted Gradium APIs
directly (no local ASR/TTS servers).

Run with: uv run uvicorn main:app --reload --port 8007
"""

from __future__ import annotations

import logging
import os
import pathlib

import fastapi
import gradbot

gradbot.init_logging()
logger = logging.getLogger("interview-questions")

APP_DIR = pathlib.Path(__file__).parent
DEFAULT_VOICE_ID = os.environ.get("INTERVIEW_VOICE_ID", "jBULVCDhf05tOJN5")
DEFAULT_LANGUAGE = "fr"

SYSTEM_PROMPT = (APP_DIR / "prompts" / "main.txt").read_text(encoding="utf-8").strip()

cfg = gradbot.config.load(APP_DIR)
app = fastapi.FastAPI(title="Notre IA")


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def make_config(msg: dict, *, speaks_first: bool = False) -> gradbot.SessionConfig:
    language = msg.get("language") or DEFAULT_LANGUAGE
    lang = gradbot.LANGUAGES.get(language) or gradbot.LANGUAGES[DEFAULT_LANGUAGE]
    prompt = (msg.get("prompt") or SYSTEM_PROMPT).strip()
    speed = _float(msg.get("speed"), 1.0)
    voice_id = msg.get("voice_id") or DEFAULT_VOICE_ID
    runtime = {
        "assistant_speaks_first": speaks_first,
        "rewrite_rules": lang.rewrite_rules,
        "padding_bonus": max(-4.0, min(4.0, -2.5 * (speed - 1.0))),
    }
    logger.info(
        "session config (lang=%s, voice=%s, speed=%.2f, speaks_first=%s)",
        language,
        voice_id,
        speed,
        speaks_first,
    )
    return gradbot.SessionConfig(
        voice_id=voice_id,
        language=lang,
        instructions=prompt,
        **(cfg.session_kwargs | runtime),
    )


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket) -> None:
    async def on_start(msg: dict) -> gradbot.SessionConfig:
        return make_config(msg, speaks_first=True)

    async def on_config(msg: dict) -> gradbot.SessionConfig:
        return make_config(msg)

    await gradbot.websocket.handle_session(
        websocket,
        config=cfg,
        on_start=on_start,
        on_config=on_config,
    )


gradbot.routes.setup(
    app,
    config=cfg,
    static_dir=APP_DIR / "static",
    with_voices=False,
)
