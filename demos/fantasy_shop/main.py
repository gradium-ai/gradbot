"""Fantasy Shop — a haggling game with voice AI.

Run with: uv run uvicorn main:app --reload
"""

import pathlib

import fastapi
import game
import gradbot

gradbot.init_logging()
app = fastapi.FastAPI(title="Fantasy Shop Demo")


@app.get("/api/game-info")
async def game_info():
    start_state = game.GameState()
    return fastapi.responses.JSONResponse(
        content={
            "title": "The Sharp Edge - Fantasy Weapon Shop",
            "goal": "Buy the legendary sword Dragonbane",
            "starting_gold": start_state.gold,
            "sword_price": start_state.sword_price,
            "inventory": [
                f"{start_state.gold} gold coins",
                "Fake ruby (looks real!)",
            ],
        }
    )


@app.websocket("/ws/game")
async def websocket_game(websocket: fastapi.WebSocket):
    state = game.GameState()

    async def on_start(msg: dict) -> gradbot.SessionConfig:
        del msg
        await websocket.send_json(state.game_state_payload())
        return game.make_config(state, speaks_first=True)

    await gradbot.websocket.handle_session(
        websocket,
        config=gradbot.config.from_env(),
        on_start=on_start,
        on_tool_call=lambda *a: game.on_tool_call(state, *a),
    )


gradbot.routes.setup(
    app,
    config=gradbot.config.from_env(),
    static_dir=pathlib.Path(__file__).parent / "static",
)
