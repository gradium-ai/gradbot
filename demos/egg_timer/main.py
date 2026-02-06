"""
Egg Timer Demo - AI assistant with timer functionality and voice changing

A FastAPI backend that exposes:
- GET /api/voices - list available flagship voices with descriptions
- WebSocket /ws/chat - real-time voice conversation with timers and voice switching

The AI can:
1. Switch between different voice personas
2. Set timers with reasons
3. Notify the user when timers expire

Run with: uvicorn main:app --reload
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pygradbot

# Initialize Rust logging (outputs to stderr)
pygradbot.init_logging()


@dataclass
class Timer:
    """Represents an active timer."""

    duration_s: int
    reason: str
    task: Optional[asyncio.Task] = None
    tool_handle: Optional[object] = (
        None  # ToolCallHandle - stored to send result when timer expires
    )


@dataclass
class SessionState:
    """Tracks the current session state."""

    current_voice: str = "Emma"
    timers: dict[str, Timer] = field(default_factory=dict)
    timer_counter: int = 0


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
        tool = pygradbot.ToolDef(
            name=f"switch_to_{voice.name.lower()}",
            description=f"Switch to {voice.name}'s voice. {voice.description}",
            parameters_json=json.dumps(
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            ),
        )
        tools.append(tool)
    return tools


def build_timer_tool() -> pygradbot.ToolDef:
    """Build tool definition for setting a timer."""
    return pygradbot.ToolDef(
        name="set_timer",
        description="Set a timer for a specific duration in seconds. The timer will notify the user when it expires.",
        parameters_json=json.dumps(
            {
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Duration in seconds (e.g., 60 for 1 minute, 300 for 5 minutes, 3600 for 1 hour)",
                        "minimum": 1,
                        "maximum": 7200,  # 2 hours max
                    },
                    "reason": {
                        "type": "string",
                        "description": "What the timer is for (e.g., 'boiling eggs', 'coffee brewing', 'pomodoro break')",
                    },
                },
                "required": ["duration_seconds", "reason"],
            }
        ),
    )


def get_system_prompt(
    current_voice_name: str, active_timers: list[Timer]
) -> str:
    """Build the system prompt for the egg timer demo."""
    voice = pygradbot.flagship_voice(current_voice_name)

    # Build timers context
    timers_context = ""
    if active_timers:
        timers_context = "\n\nACTIVE TIMERS:\n"
        for timer in active_timers:
            timers_context += (
                f"- {timer.reason}: {timer.duration_s} seconds remaining\n"
            )
        timers_context += "\nWhen a timer expires, you will receive a notification and should inform the user."
    else:
        timers_context = "\n\nNO ACTIVE TIMERS"

    return f"""You are {voice.name}, a friendly AI assistant who loves helping with timers.

{voice.description}

YOUR CAPABILITIES:
1. You can switch between different voice personas - use the switch_to_* tools to change your voice
2. You can set timers for the user with specific durations and reasons
3. When a timer expires, you'll be notified and should inform the user{timers_context}

TIMER USAGE:
- When the user asks for a timer (e.g., "set a timer for 5 minutes" or "remind me in 30 seconds"), use the set_timer tool
- Always ask for or infer the REASON for the timer (what is it for?)
- Examples: "Set a timer for boiling eggs", "Remind me to check the oven in 10 minutes"
- After setting a timer, confirm the details: "Timer set for 5 minutes for boiling eggs"

WHILE WAITING FOR TIMERS - KEEP TALKING!:
- This is CRITICAL: After setting a timer, the tool call stays open and will return when the timer expires
- While waiting, CONTINUE THE CONVERSATION with chit-chat! Ask questions, tell jokes, share fun facts
- NEVER go silent after setting a timer - that's awkward! Keep chatting with the user
- Suggested topics: Ask about their day, share a fun fact, tell a short joke, ask about their favorite foods, etc.
- Example: "Timer set for your eggs! While we wait, do you like them soft-boiled or hard-boiled?"
- The timer will automatically notify you when it expires, so don't worry about watching the clock

VOICE SWITCHING:
- Feel free to switch voices for fun or when the user asks
- Each voice has a different personality - embrace it!
- Popular voices: Emma (warm US), Leo (charming French), Kent (professional US)

CONVERSATION STYLE:
- Keep responses conversational and friendly
- Feel free to make small talk between timer requests
- If multiple timers are active, you can mention them occasionally
- When a timer expires, make it clear and offer to set another one if needed

Start by greeting the user and asking how you can help with timers today!
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Egg Timer Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Egg Timer Demo", lifespan=lifespan)


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
    WebSocket endpoint for voice chat with timers and voice switching.

    Protocol:
    - Client sends JSON: {"type": "start", "voice_name": "Emma"}
    - Client sends binary: raw audio data (PCM 16-bit, 24kHz, mono)
    - Server sends JSON: {"type": "transcript", "text": "...", "is_user": true/false}
    - Server sends JSON: {"type": "voice_change", "voice_name": "..."}
    - Server sends JSON: {"type": "timer_set", "timer_id": "...", "duration_s": 300, "reason": "..."}
    - Server sends JSON: {"type": "timer_expired", "timer_id": "...", "reason": "..."}
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
            await websocket.close(
                code=4001, reason=f"Unknown voice: {voice_name}"
            )
            return

        # Initialize session state
        state = SessionState(current_voice=voice_name)
        print(f"Starting egg timer chat with voice={voice_name}")

        # Build tools for voice switching and timers
        voice_tools = build_voice_tools()
        timer_tool = build_timer_tool()
        tools = voice_tools + [timer_tool]
        print(
            f"Built {len(tools)} tools: {len(voice_tools)} voice tools + 1 timer tool"
        )

        # Create session config
        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(voice_name, []),
            language=voice.language,
            tools=tools,
        )

        # Create clients and start session
        input_handle, output_handle = await pygradbot.run(
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_timer_expiration(timer_id: str, timer: Timer):
            """Handle when a timer expires - send result via tool_handle."""
            await asyncio.sleep(timer.duration_s)

            if timer_id in state.timers:
                # Remove from active timers
                del state.timers[timer_id]

                print(f"Timer expired: {timer.reason} ({timer.duration_s}s)")

                # Notify client
                await websocket.send_json(
                    {
                        "type": "timer_expired",
                        "timer_id": timer_id,
                        "reason": timer.reason,
                        "duration_s": timer.duration_s,
                    }
                )

                # Send timer completion result to LLM via the stored tool_handle
                if timer.tool_handle:
                    await timer.tool_handle.send(
                        json.dumps(
                            {
                                "success": True,
                                "timer_id": timer_id,
                                "reason": timer.reason,
                                "duration_s": timer.duration_s,
                                "message": f"The timer for '{timer.reason}' has expired after {timer.duration_s} seconds. Please inform the user that their timer is done.",
                            }
                        )
                    )

        async def handle_tool_call(tool_call, tool_handle):
            """Handle voice switching and timer tool calls."""
            nonlocal state, tools

            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)

            print(f"Tool call: {tool_name} - {args}")

            # Handle voice switching
            if tool_name.startswith("switch_to_"):
                new_voice_name = tool_name[len("switch_to_") :].capitalize()
                # Handle multi-word names
                for v in pygradbot.flagship_voices():
                    if v.name.lower() == tool_name[len("switch_to_") :]:
                        new_voice_name = v.name
                        break

                try:
                    new_voice = pygradbot.flagship_voice(new_voice_name)
                    state.current_voice = new_voice_name

                    # Update session config with new voice
                    active_timers = list(state.timers.values())
                    new_config = pygradbot.SessionConfig(
                        voice_id=new_voice.voice_id,
                        instructions=get_system_prompt(
                            new_voice_name, active_timers
                        ),
                        language=new_voice.language,
                        tools=tools,
                    )
                    await input_handle.send_config(new_config)

                    # Notify client of voice change
                    await websocket.send_json(
                        {
                            "type": "voice_change",
                            "voice_name": new_voice_name,
                            "description": new_voice.description,
                        }
                    )

                    # Send success to LLM
                    await tool_handle.send(
                        json.dumps(
                            {
                                "success": True,
                                "message": f"Voice switched to {new_voice_name}. You are now speaking with this voice and personality.",
                            }
                        )
                    )
                except RuntimeError as e:
                    await tool_handle.send_error(str(e))

            # Handle timer setting
            elif tool_name == "set_timer":
                duration_s = args.get("duration_seconds", 60)
                reason = args.get("reason", "unnamed timer")

                # Create timer ID
                state.timer_counter += 1
                timer_id = f"timer_{state.timer_counter}"

                # Create timer object with tool_handle stored
                timer = Timer(
                    duration_s=duration_s,
                    reason=reason,
                    tool_handle=tool_handle,
                )
                state.timers[timer_id] = timer

                # Start expiration task (result will be sent when timer expires)
                timer.task = asyncio.create_task(
                    handle_timer_expiration(timer_id, timer)
                )

                print(f"Timer set: {timer_id} - {reason} ({duration_s}s)")

                # Notify client
                await websocket.send_json(
                    {
                        "type": "timer_set",
                        "timer_id": timer_id,
                        "duration_s": duration_s,
                        "reason": reason,
                        "active_timers": len(state.timers),
                    }
                )

                # DON'T send result yet - it will be sent when timer expires via tool_handle

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
                        await websocket.send_json(
                            {
                                "type": "audio_timing",
                                "start_s": msg.start_s,
                                "stop_s": msg.stop_s,
                                "turn_idx": msg.turn_idx,
                                "interrupted": msg.interrupted,
                            }
                        )
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "text": msg.text,
                                "is_user": False,
                                "stop_s": msg.stop_s,
                                "turn_idx": msg.turn_idx,
                            }
                        )

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "text": msg.text,
                                "is_user": True,
                            }
                        )

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(
                            handle_tool_call(
                                msg.tool_call, msg.tool_call_handle
                            )
                        )

                    elif msg.msg_type == "event":
                        await websocket.send_json(
                            {
                                "type": "event",
                                "event": msg.event.event_type,
                            }
                        )

                except Exception as e:
                    print(f"Output processing error: {e}")
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": str(e),
                            }
                        )
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
    finally:
        # Cancel all active timer tasks
        try:
            for timer in state.timers.values():
                if timer.task and not timer.task.done():
                    timer.task.cancel()
        except:
            pass
        try:
            await websocket.close()
        except:
            pass


# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount(
        "/static",
        StaticFiles(directory=static_dir, follow_symlink=True),
        name="static",
    )


@app.get("/")
async def index():
    """Serve the main page."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse(
        content={"error": "Frontend not found. Place index.html in static/"},
        status_code=404,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
