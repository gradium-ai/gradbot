"""
Voice Text Adventure - Play classic text adventures with voice AI

A FastAPI backend that integrates Jericho text adventure engine with Gradbot voice AI.
Users can play games like Zork using voice commands.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import jericho
import gradbot
from gradbot.fastapi import websocket_chat_handler

# Initialize Rust logging
gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

logger = logging.getLogger(__name__)

# Games directory
GAMES_DIR = Path(__file__).parent / "games"

# Language mappings
LANG_MAP = {
    "en": gradbot.Lang.En,
    "fr": gradbot.Lang.Fr,
    "de": gradbot.Lang.De,
    "es": gradbot.Lang.Es,
    "pt": gradbot.Lang.Pt,
}

LANG_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
}


LANG_TO_CODE = {
    gradbot.Lang.En: "en",
    gradbot.Lang.Fr: "fr",
    gradbot.Lang.De: "de",
    gradbot.Lang.Es: "es",
    gradbot.Lang.Pt: "pt",
}


def get_voices_by_language() -> dict[str, list[dict]]:
    """Get all flagship voices organized by language."""
    voices_by_lang = {}
    for voice in gradbot.flagship_voices():
        lang_code = LANG_TO_CODE.get(voice.language, "en")

        if lang_code not in voices_by_lang:
            voices_by_lang[lang_code] = []

        voices_by_lang[lang_code].append({
            "name": voice.name,
            "voice_id": voice.voice_id,
            "gender": str(voice.gender),
            "description": voice.description,
        })

    return voices_by_lang


def get_all_voice_names() -> list[str]:
    """Get list of all available voice names."""
    return [v.name for v in gradbot.flagship_voices()]


@dataclass
class GameState:
    """Tracks the current state of the game session."""
    game: jericho.FrotzEnv | None = None
    game_name: str = ""
    current_description: str = ""
    valid_actions: list[str] = field(default_factory=list)
    score: int = 0
    moves: int = 0
    inventory: list[str] = field(default_factory=list)
    game_over: bool = False
    # Voice/language state
    language: str = "en"
    voice_name: str = "John"
    narrator_style: str = "dramatic"  # dramatic, spooky, comedic, etc.


def get_available_games() -> list[dict]:
    """List available Jericho games in the games directory."""
    games = []
    if GAMES_DIR.exists():
        for game_file in GAMES_DIR.glob("*.z*"):
            games.append({
                "filename": game_file.name,
                "name": game_file.stem.replace("_", " ").title(),
                "path": str(game_file),
            })
    return sorted(games, key=lambda g: g["name"])


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_NARRATOR_PROMPT_TEMPLATE = (_PROMPTS_DIR / "narrator.txt").read_text()


def get_narrator_prompt(state: GameState) -> str:
    """System prompt for the voice narrator/game master."""
    valid_actions_str = ", ".join(state.valid_actions[:20]) if state.valid_actions else "explore, look, examine"
    lang_name = LANG_NAMES.get(state.language, "English")
    all_voices = get_all_voice_names()

    return _NARRATOR_PROMPT_TEMPLATE.format(
        narrator_style=state.narrator_style,
        game_name=state.game_name,
        current_description=state.current_description[:500] if state.current_description else "Game starting...",
        score=state.score,
        moves=state.moves,
        valid_actions_str=valid_actions_str,
        voice_name=state.voice_name,
        lang_name=lang_name,
        all_voices=', '.join(all_voices),
    )


def build_game_tools() -> list[gradbot.ToolDef]:
    """Tools available to the voice narrator."""
    all_voices = get_all_voice_names()

    return [
        gradbot.ToolDef(
            name="execute_command",
            description="Execute a command in the text adventure game. Use this for any player action like movement (go north, south, east, west), object interaction (take, drop, open, examine), or other game commands.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute (e.g., 'go north', 'take lamp', 'examine mailbox')"
                    }
                },
                "required": ["command"]
            }),
        ),
        gradbot.ToolDef(
            name="get_valid_commands",
            description="Get a list of currently valid commands the player can use. Helpful when the player is stuck or asking what they can do.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        gradbot.ToolDef(
            name="get_game_state",
            description="Get the current game state including location description, score, and moves. Use this to remind yourself or the player of the current situation.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        gradbot.ToolDef(
            name="change_language",
            description="Change the narration language. Use when the player speaks in a different language. You are multilingual! Supported: en (English), fr (French), de (German), es (Spanish), pt (Portuguese).",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "fr", "de", "es", "pt"],
                        "description": "The language code to switch to"
                    }
                },
                "required": ["language"]
            }),
        ),
        gradbot.ToolDef(
            name="change_voice",
            description=f"Change the narrator voice for dramatic effect. Use creatively: spooky voice for dark places, different voice for reading inscriptions, etc. Available voices: {', '.join(all_voices)}",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "voice_name": {
                        "type": "string",
                        "enum": all_voices,
                        "description": "The name of the voice to switch to"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you're changing the voice (for dramatic effect, mood change, etc.)"
                    }
                },
                "required": ["voice_name"]
            }),
        ),
        gradbot.ToolDef(
            name="set_narrator_style",
            description="Change the narration style/mood. Use to match the game's atmosphere.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "style": {
                        "type": "string",
                        "enum": ["dramatic", "spooky", "comedic", "mysterious", "heroic", "whimsical"],
                        "description": "The narration style to use"
                    }
                },
                "required": ["style"]
            }),
        ),
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Voice Text Adventure Demo...")
    games = get_available_games()
    logger.info("Found %d games: %s", len(games), [g['name'] for g in games])
    voices = get_all_voice_names()
    logger.info("Available voices: %s", voices)
    yield
    logger.info("Shutting down...")


app = FastAPI(title="Voice Text Adventure", lifespan=lifespan)


@app.get("/api/games")
async def list_games():
    """Return list of available text adventure games."""
    games = get_available_games()
    return JSONResponse(content={"games": games})


@app.get("/api/voices")
async def list_voices():
    """Return list of available voices by language."""
    voices = get_voices_by_language()
    return JSONResponse(content={"voices": voices})


@app.websocket("/ws/game")
async def websocket_game(websocket: WebSocket):
    """WebSocket endpoint for the game session."""

    # Per-session state
    state = GameState()
    tools = build_game_tools()
    game_executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()

    async def on_start(msg: dict) -> gradbot.SessionConfig:
        game_filename = msg.get("game")
        if not game_filename:
            raise RuntimeError("No game specified")

        # Find and load the game
        game_path = GAMES_DIR / game_filename
        if not game_path.exists():
            raise RuntimeError(f"Game not found: {game_filename}")

        logger.info("Loading game: %s", game_path)

        def _init_game():
            game = jericho.FrotzEnv(str(game_path))
            obs, info = game.reset()
            valid = game.get_valid_actions()
            return game, obs, info, valid

        state.game, initial_obs, info, state.valid_actions = await loop.run_in_executor(
            game_executor, _init_game
        )
        state.game_name = game_path.stem.replace("_", " ").title()
        state.current_description = initial_obs
        state.score = info.get("score", 0)
        state.moves = info.get("moves", 0)

        logger.info("Game loaded. Initial state: score=%d, moves=%d", state.score, state.moves)
        logger.info("Valid actions: %s...", state.valid_actions[:10])

        # Send initial game state to client
        await websocket.send_json({
            "type": "game_state",
            "state": {
                "game_name": state.game_name,
                "description": state.current_description,
                "score": state.score,
                "moves": state.moves,
                "valid_actions": state.valid_actions[:20],
                "game_over": False,
                "language": state.language,
                "voice_name": state.voice_name,
            }
        })

        # Get initial voice for the narrator
        voice = gradbot.flagship_voice(state.voice_name)

        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_narrator_prompt(state),
            language=LANG_MAP[state.language],
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=LANG_MAP[state.language].rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

    async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
        """Handle game tool calls."""
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        call_id = getattr(tool_call, "call_id", "unknown")

        logger.info("Tool call start: %s %s - %s", call_id, tool_name, args)

        if tool_name == "execute_command":
            command = args.get("command", "look")

            if state.game is None:
                await tool_handle.send(json.dumps({
                    "error": "No game loaded"
                }))
                logger.info("Tool call done: %s %s (no game)", call_id, tool_name)
                return

            try:
                # Execute the command on a dedicated single thread (Jericho is not thread-safe)
                obs, reward, done, info = await loop.run_in_executor(
                    game_executor, state.game.step, command
                )

                # Update state
                state.current_description = obs
                state.score = info.get("score", state.score)
                state.moves = info.get("moves", state.moves)
                state.game_over = done

                # Return result to the voice agent FIRST (before slow get_valid_actions)
                await tool_handle.send(json.dumps({
                    "command": command,
                    "result": obs,
                    "score": state.score,
                    "moves": state.moves,
                    "game_over": done,
                    "reward": reward,
                }))
                logger.info("Tool call done: %s %s (ok)", call_id, tool_name)

                # Get new valid actions (slow - uses multiprocessing, run in executor)
                state.valid_actions = await loop.run_in_executor(
                    game_executor, state.game.get_valid_actions
                )

                # Send updated state to client
                await websocket.send_json({
                    "type": "game_state",
                    "state": {
                        "game_name": state.game_name,
                        "description": state.current_description,
                        "score": state.score,
                        "moves": state.moves,
                        "valid_actions": state.valid_actions[:20],
                        "game_over": state.game_over,
                        "language": state.language,
                        "voice_name": state.voice_name,
                    }
                })

                if state.game_over:
                    await websocket.send_json({
                        "type": "game_over",
                        "message": "The game has ended.",
                        "final_score": state.score,
                    })

            except Exception as e:
                logger.error("Command execution error: %s", e)
                await tool_handle.send(json.dumps({
                    "error": str(e),
                    "command": command,
                }))
                logger.info("Tool call done: %s %s (error)", call_id, tool_name)

        elif tool_name == "get_valid_commands":
            await tool_handle.send(json.dumps({
                "valid_commands": state.valid_actions[:30],
                "total_count": len(state.valid_actions),
            }))
            logger.info("Tool call done: %s %s (ok)", call_id, tool_name)

        elif tool_name == "get_game_state":
            await tool_handle.send(json.dumps({
                "game_name": state.game_name,
                "description": state.current_description,
                "score": state.score,
                "moves": state.moves,
                "game_over": state.game_over,
            }))
            logger.info("Tool call done: %s %s (ok)", call_id, tool_name)

        elif tool_name == "change_language":
            new_lang = args.get("language", "en")
            if new_lang in LANG_MAP:
                state.language = new_lang
                lang_name = LANG_NAMES.get(new_lang, "English")

                # Get a voice that matches the new language if possible
                voices_by_lang = get_voices_by_language()
                if new_lang in voices_by_lang and voices_by_lang[new_lang]:
                    # Pick first voice of that language
                    state.voice_name = voices_by_lang[new_lang][0]["name"]

                voice = gradbot.flagship_voice(state.voice_name)
                new_config = gradbot.SessionConfig(
                    voice_id=voice.voice_id,
                    instructions=get_narrator_prompt(state),
                    language=LANG_MAP[new_lang],
                    tools=tools,
                    **merge_overrides(_OVERRIDES,
                        flush_duration_s=FLUSH_FOR_S,
                        rewrite_rules=LANG_MAP[new_lang].rewrite_rules,
                    ),
                )
                await input_handle.send_config(new_config)

                await websocket.send_json({
                    "type": "narrator_change",
                    "language": new_lang,
                    "language_name": lang_name,
                    "voice_name": state.voice_name,
                })

                await tool_handle.send(json.dumps({
                    "result": f"Switched to {lang_name}. Now narrating as {state.voice_name}.",
                    "language": new_lang,
                    "voice_name": state.voice_name,
                }))
                logger.info("Tool call done: %s %s (ok)", call_id, tool_name)
            else:
                await tool_handle.send_error(f"Unknown language: {new_lang}")
                logger.info("Tool call done: %s %s (error)", call_id, tool_name)

        elif tool_name == "change_voice":
            voice_name = args.get("voice_name")
            reason = args.get("reason", "dramatic effect")

            try:
                voice = gradbot.flagship_voice(voice_name)
                state.voice_name = voice_name

                new_config = gradbot.SessionConfig(
                    voice_id=voice.voice_id,
                    instructions=get_narrator_prompt(state),
                    language=LANG_MAP[state.language],
                    tools=tools,
                    **merge_overrides(_OVERRIDES,
                        flush_duration_s=FLUSH_FOR_S,
                        rewrite_rules=LANG_MAP[state.language].rewrite_rules,
                    ),
                )
                await input_handle.send_config(new_config)

                await websocket.send_json({
                    "type": "narrator_change",
                    "voice_name": voice_name,
                    "reason": reason,
                })

                await tool_handle.send(json.dumps({
                    "result": f"Voice changed to {voice_name}.",
                    "voice_name": voice_name,
                }))
                logger.info("Tool call done: %s %s (ok)", call_id, tool_name)
            except Exception as e:
                await tool_handle.send_error(f"Unknown voice: {voice_name}. Error: {e}")
                logger.info("Tool call done: %s %s (error)", call_id, tool_name)

        elif tool_name == "set_narrator_style":
            style = args.get("style", "dramatic")
            state.narrator_style = style

            # Update prompt with new style
            voice = gradbot.flagship_voice(state.voice_name)
            new_config = gradbot.SessionConfig(
                voice_id=voice.voice_id,
                instructions=get_narrator_prompt(state),
                language=LANG_MAP[state.language],
                tools=tools,
                **merge_overrides(_OVERRIDES,
                    flush_duration_s=FLUSH_FOR_S,
                    rewrite_rules=LANG_MAP[state.language].rewrite_rules,
                ),
            )
            await input_handle.send_config(new_config)

            await websocket.send_json({
                "type": "narrator_change",
                "style": style,
            })

            await tool_handle.send(json.dumps({
                "result": f"Narration style changed to {style}.",
                "style": style,
            }))
            logger.info("Tool call done: %s %s (ok)", call_id, tool_name)

        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")
            logger.info("Tool call done: %s %s (error)", call_id, tool_name)

    try:
        await websocket_chat_handler(
            websocket,
            on_start=on_start,
            on_tool_call=handle_tool_call,
            run_kwargs=_CLIENT_CONFIG,
            output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
            debug=DEBUG,
        )
    finally:
        # Clean up game
        if state.game:
            try:
                state.game.close()
            except Exception:
                pass
        try:
            game_executor.shutdown(wait=False)
        except Exception:
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
