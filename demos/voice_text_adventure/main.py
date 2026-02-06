"""
Voice Text Adventure - Play classic text adventures with voice AI

A FastAPI backend that integrates Jericho text adventure engine with Gradbot voice AI.
Users can play games like Zork using voice commands.

Run with: uvicorn main:app --reload
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import jericho
import pygradbot

# Initialize Rust logging
pygradbot.init_logging()

# Games directory
GAMES_DIR = Path(__file__).parent / "games"

# Language mappings
LANG_MAP = {
    "en": pygradbot.Lang.En,
    "fr": pygradbot.Lang.Fr,
    "de": pygradbot.Lang.De,
    "es": pygradbot.Lang.Es,
    "pt": pygradbot.Lang.Pt,
}

LANG_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
}


LANG_TO_CODE = {
    pygradbot.Lang.En: "en",
    pygradbot.Lang.Fr: "fr",
    pygradbot.Lang.De: "de",
    pygradbot.Lang.Es: "es",
    pygradbot.Lang.Pt: "pt",
}


def get_voices_by_language() -> dict[str, list[dict]]:
    """Get all flagship voices organized by language."""
    voices_by_lang = {}
    for voice in pygradbot.flagship_voices():
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
    return [v.name for v in pygradbot.flagship_voices()]


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
    voice_name: str = "Kent"
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


def get_narrator_prompt(state: GameState) -> str:
    """System prompt for the voice narrator/game master."""
    valid_actions_str = ", ".join(state.valid_actions[:20]) if state.valid_actions else "explore, look, examine"
    lang_name = LANG_NAMES.get(state.language, "English")
    all_voices = get_all_voice_names()

    return f"""You are a {state.narrator_style} narrator for the text adventure game "{state.game_name}". You read game descriptions aloud and help the player navigate the game world.

CURRENT GAME STATE:
- Location description: {state.current_description[:500] if state.current_description else "Game starting..."}
- Score: {state.score} | Moves: {state.moves}
- Some valid commands: {valid_actions_str}

CURRENT VOICE: {state.voice_name}
CURRENT LANGUAGE: {lang_name}
AVAILABLE VOICES: {', '.join(all_voices)}

YOUR ROLE:
1. When the game starts or after commands, READ the game's description aloud dramatically
2. Listen to player commands and execute them using the execute_command tool
3. If the player's speech doesn't match a valid command, help them by suggesting similar commands
4. TRANSLATE game text to the current language ({lang_name}) - the game is in English but you narrate in {lang_name}
5. Add atmosphere but don't make up game content - stick to what the game provides

SPEAKING STYLE:
- Keep responses concise (2-3 sentences for descriptions)
- Use a {state.narrator_style} narrator voice appropriate for the mood
- NEVER use action annotations like *looks around* - just speak naturally
- Read game text verbatim (translated to {lang_name}) but feel free to add brief dramatic flair
- ALWAYS speak in {lang_name}!

VOICE & LANGUAGE TOOLS:
- change_language: If the player speaks in another language, switch to match them!
- change_voice: Change narrator voice for dramatic effect (spooky scenes, different characters, etc.)
  Use this creatively - maybe a different voice for reading signs, or a spookier voice in dark places.

GAME TOOLS:
- execute_command: Run a command in the game (go north, take lamp, open door, etc.)
- get_valid_commands: See the list of currently valid commands
- get_game_state: Get the current game description and status

IMPORTANT:
- Always use execute_command to send commands to the game
- If the player says something that sounds like a command, try to execute it
- For ambiguous speech, ask for clarification or suggest valid commands
- Keep the game moving - don't over-explain, let the player explore!
- Feel free to change your voice to match the mood (spooky dungeon = deeper voice, etc.)

Start by reading the current location description to the player in {lang_name}."""


def build_game_tools() -> list[pygradbot.ToolDef]:
    """Tools available to the voice narrator."""
    all_voices = get_all_voice_names()

    return [
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
            name="get_valid_commands",
            description="Get a list of currently valid commands the player can use. Helpful when the player is stuck or asking what they can do.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        pygradbot.ToolDef(
            name="get_game_state",
            description="Get the current game state including location description, score, and moves. Use this to remind yourself or the player of the current situation.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
    print("Starting Voice Text Adventure Demo...")
    games = get_available_games()
    print(f"Found {len(games)} games: {[g['name'] for g in games]}")
    voices = get_all_voice_names()
    print(f"Available voices: {voices}")
    yield
    print("Shutting down...")


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
    await websocket.accept()

    # Initialize game state
    state = GameState()
    input_handle = None
    tools = build_game_tools()

    try:
        # Wait for start message with game selection
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        game_filename = start_msg.get("game")
        if not game_filename:
            await websocket.close(code=4001, reason="No game specified")
            return

        # Find and load the game
        game_path = GAMES_DIR / game_filename
        if not game_path.exists():
            await websocket.close(code=4002, reason=f"Game not found: {game_filename}")
            return

        print(f"Loading game: {game_path}")
        state.game = jericho.FrotzEnv(str(game_path))
        state.game_name = game_path.stem.replace("_", " ").title()

        # Get initial game state
        initial_obs, info = state.game.reset()
        state.current_description = initial_obs
        state.score = info.get("score", 0)
        state.moves = info.get("moves", 0)

        # Get valid actions
        state.valid_actions = state.game.get_valid_actions()

        print(f"Game loaded. Initial state: score={state.score}, moves={state.moves}")
        print(f"Valid actions: {state.valid_actions[:10]}...")

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
        voice = pygradbot.flagship_voice(state.voice_name)

        # Create session with narrator tools
        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_narrator_prompt(state),
            language=LANG_MAP[state.language],
            tools=tools,
        )

        input_handle, output_handle = await pygradbot.run(
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()
        game_executor = ThreadPoolExecutor(max_workers=1)
        tool_task_counter = 0

        async def handle_tool_call(tool_call, tool_handle):
            """Handle game tool calls."""
            nonlocal state, tools
            call_id = getattr(tool_call, "call_id", "unknown")

            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)

            print(f"Tool call start: {call_id} {tool_name} - {args}")

            if tool_name == "execute_command":
                command = args.get("command", "look")

                if state.game is None:
                    await tool_handle.send(json.dumps({
                        "error": "No game loaded"
                    }))
                    print(f"Tool call done: {call_id} {tool_name} (no game)")
                    return

                try:
                    # Execute the command on a dedicated single thread (Jericho is not thread-safe)
                    loop = asyncio.get_running_loop()
                    obs, reward, done, info = await loop.run_in_executor(
                        game_executor, state.game.step, command
                    )

                    # Update state
                    state.current_description = obs
                    state.score = info.get("score", state.score)
                    state.moves = info.get("moves", state.moves)
                    state.game_over = done

                    # Get new valid actions
                    state.valid_actions = state.game.get_valid_actions()

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

                    # Return result to the voice agent
                    await tool_handle.send(json.dumps({
                        "command": command,
                        "result": obs,
                        "score": state.score,
                        "moves": state.moves,
                        "game_over": done,
                        "reward": reward,
                    }))
                    print(f"Tool call done: {call_id} {tool_name} (ok)")

                except Exception as e:
                    print(f"Command execution error: {e}")
                    await tool_handle.send(json.dumps({
                        "error": str(e),
                        "command": command,
                    }))
                    print(f"Tool call done: {call_id} {tool_name} (error)")

            elif tool_name == "get_valid_commands":
                await tool_handle.send(json.dumps({
                    "valid_commands": state.valid_actions[:30],
                    "total_count": len(state.valid_actions),
                }))
                print(f"Tool call done: {call_id} {tool_name} (ok)")

            elif tool_name == "get_game_state":
                await tool_handle.send(json.dumps({
                    "game_name": state.game_name,
                    "description": state.current_description,
                    "score": state.score,
                    "moves": state.moves,
                    "game_over": state.game_over,
                }))
                print(f"Tool call done: {call_id} {tool_name} (ok)")

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

                    voice = pygradbot.flagship_voice(state.voice_name)
                    new_config = pygradbot.SessionConfig(
                        voice_id=voice.voice_id,
                        instructions=get_narrator_prompt(state),
                        language=LANG_MAP[new_lang],
                        tools=tools,
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
                    print(f"Tool call done: {call_id} {tool_name} (ok)")
                else:
                    await tool_handle.send_error(f"Unknown language: {new_lang}")
                    print(f"Tool call done: {call_id} {tool_name} (error)")

            elif tool_name == "change_voice":
                voice_name = args.get("voice_name")
                reason = args.get("reason", "dramatic effect")

                try:
                    voice = pygradbot.flagship_voice(voice_name)
                    state.voice_name = voice_name

                    new_config = pygradbot.SessionConfig(
                        voice_id=voice.voice_id,
                        instructions=get_narrator_prompt(state),
                        language=LANG_MAP[state.language],
                        tools=tools,
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
                    print(f"Tool call done: {call_id} {tool_name} (ok)")
                except Exception as e:
                    await tool_handle.send_error(f"Unknown voice: {voice_name}. Error: {e}")
                    print(f"Tool call done: {call_id} {tool_name} (error)")

            elif tool_name == "set_narrator_style":
                style = args.get("style", "dramatic")
                state.narrator_style = style

                # Update prompt with new style
                voice = pygradbot.flagship_voice(state.voice_name)
                new_config = pygradbot.SessionConfig(
                    voice_id=voice.voice_id,
                    instructions=get_narrator_prompt(state),
                    language=LANG_MAP[state.language],
                    tools=tools,
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
                print(f"Tool call done: {call_id} {tool_name} (ok)")

            else:
                await tool_handle.send_error(f"Unknown tool: {tool_name}")
                print(f"Tool call done: {call_id} {tool_name} (error)")

        def _log_tool_task_done(task, call_id, tool_name):
            try:
                task.result()
            except Exception as e:
                print(f"Tool call task error: {call_id} {tool_name} - {e}")
            else:
                print(f"Tool call task finished: {call_id} {tool_name}")

        async def process_output():
            """Receive output from gradbot and send to client."""
            nonlocal tool_task_counter
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
                        tool_task_counter += 1
                        call_id = getattr(msg.tool_call, "call_id", "unknown")
                        tool_name = msg.tool_call.tool_name
                        print(f"Tool call queued: {call_id} {tool_name} task={tool_task_counter}")
                        task = asyncio.create_task(handle_tool_call(msg.tool_call, msg.tool_call_handle))
                        task.add_done_callback(lambda t, cid=call_id, tn=tool_name: _log_tool_task_done(t, cid, tn))

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    import traceback
                    traceback.print_exc()
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

        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
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
        try:
            await websocket.close()
        except Exception:
            pass


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
