"""Local voice research demo. Run: uv run uvicorn main:app --host 127.0.0.1."""

import os
from pathlib import Path

import fastapi
import gradbot

from flashdata_agent import ResearchState, make_config, on_tool_call
from flashdata_client import FlashDataClient

app = fastapi.FastAPI(title="FlashData Voice Research")
cfg = gradbot.config.from_env()


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket):
    key = os.environ.get("FLASHDATA_API_KEY")
    if not key or not cfg.llm.api_key or not cfg.gradium.api_key or not cfg.llm.model:
        await websocket.close(
            code=1008,
            reason="Configure FlashData, model and voice credentials locally.",
        )
        return
    state = ResearchState(FlashDataClient(key))
    await gradbot.websocket.handle_session(
        websocket,
        config=cfg.model_copy(update={"debug": False}),
        on_start=lambda msg: make_config(state, cfg, msg),
        on_tool_call=lambda *args: on_tool_call(state, *args),
    )


gradbot.routes.setup(app, config=cfg, static_dir=Path(__file__).parent / "static")
