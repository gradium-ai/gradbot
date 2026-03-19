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
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket

import gradbot
from gradbot.fastapi import websocket_chat_handler, setup_demo_routes

# Initialize Rust logging (outputs to stderr)
gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

from gradbot.demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

logger = logging.getLogger(__name__)


@dataclass
class Timer:
    """Represents an active timer."""

    duration_s: int
    reason: str


@dataclass
class SessionState:
    """Tracks the current session state."""

    current_voice: str = "Emma"
    timers: dict[str, Timer] = field(default_factory=dict)
    timer_counter: int = 0


def build_timer_tool() -> gradbot.ToolDef:
    """Build tool definition for setting a timer."""
    return gradbot.ToolDef(
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


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SYSTEM_PROMPT_TEMPLATE = (_PROMPTS_DIR / "system.txt").read_text()


def get_system_prompt(
    current_voice_name: str, active_timers: list[Timer]
) -> str:
    """Build the system prompt for the egg timer demo."""
    voice = gradbot.flagship_voice(current_voice_name)

    # Build timers context
    if active_timers:
        timers_context = "\n\nACTIVE TIMERS:\n"
        for timer in active_timers:
            timers_context += (
                f"- {timer.reason}: {timer.duration_s} seconds remaining\n"
            )
        timers_context += "\nWhen a timer expires, you will receive a notification and should inform the user."
    else:
        timers_context = "\n\nNO ACTIVE TIMERS"

    return _SYSTEM_PROMPT_TEMPLATE.format(
        voice_name=voice.name,
        voice_description=voice.description,
        timers_context=timers_context,
    )


app = FastAPI(title="Egg Timer Demo")


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    state = SessionState()
    voice_tools = gradbot.voice_switching_tools()
    timer_tool = build_timer_tool()
    tools = voice_tools + [timer_tool]

    def make_config(voice_name: str) -> gradbot.SessionConfig:
        voice = gradbot.flagship_voice(voice_name)
        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(voice_name, list(state.timers.values())),
            language=voice.language,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=voice.language.rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

    async def on_tool_call(tool_call, tool_handle, input_handle, websocket):
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)

        logger.info("Tool call: %s - %s", tool_name, args)

        # Handle voice switching
        if tool_name.startswith("switch_to_"):
            voice = gradbot.resolve_voice_from_tool(tool_name)
            if voice is None:
                await tool_handle.send_error(f"Unknown voice: {tool_name}")
                return
            new_voice_name = voice.name

            try:
                state.current_voice = new_voice_name

                # Update session config with new voice (includes active timers)
                new_config = gradbot.SessionConfig(
                    voice_id=voice.voice_id,
                    instructions=get_system_prompt(
                        new_voice_name, list(state.timers.values())
                    ),
                    language=voice.language,
                    tools=tools,
                    **merge_overrides(_OVERRIDES,
                        flush_duration_s=FLUSH_FOR_S,
                        rewrite_rules=voice.language.rewrite_rules,
                    ),
                )
                await input_handle.send_config(new_config)

                # Notify client of voice change
                await websocket.send_json(
                    {
                        "type": "voice_change",
                        "voice_name": new_voice_name,
                        "description": voice.description,
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

            # Create timer object
            timer = Timer(duration_s=duration_s, reason=reason)
            state.timers[timer_id] = timer

            logger.info("Timer set: %s - %s (%ds)", timer_id, reason, duration_s)

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

            # Sleep until timer expires (this task is tracked and cancelled on session end)
            await asyncio.sleep(duration_s)

            # Timer expired
            if timer_id in state.timers:
                del state.timers[timer_id]

                logger.info("Timer expired: %s (%ds)", reason, duration_s)

                # Notify client
                await websocket.send_json(
                    {
                        "type": "timer_expired",
                        "timer_id": timer_id,
                        "reason": reason,
                        "duration_s": duration_s,
                    }
                )

                # Send timer completion result to LLM
                await tool_handle.send(
                    json.dumps(
                        {
                            "success": True,
                            "timer_id": timer_id,
                            "reason": reason,
                            "duration_s": duration_s,
                            "message": f"The timer for '{reason}' has expired after {duration_s} seconds. Please inform the user that their timer is done.",
                        }
                    )
                )

        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")

    await websocket_chat_handler(
        websocket,
        on_start=lambda msg: make_config(msg.get("voice_name", "Emma")),
        on_tool_call=on_tool_call,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


setup_demo_routes(app, static_dir=Path(__file__).parent / "static", use_pcm=USE_PCM, voices=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
