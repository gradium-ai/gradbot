"""FlashData research tools and Gradbot session configuration."""

import dataclasses
import json
from pathlib import Path

import gradbot

from flashdata_client import FlashDataClient

PROMPT = (Path(__file__).parent / "prompts/main.txt").read_text()
TOOLS = [
    gradbot.ToolDef(
        "flashdata_catalog",
        "Discover available FlashData research tools and input schemas before your first query. "
        "This reads metadata without submitting data queries.",
        json.dumps({"type": "object", "properties": {}}),
    ),
    gradbot.ToolDef(
        "flashdata_call",
        "Call a discovered query or supporting read once, after the user approves the "
        "scope and Credits cost. Use exact schema arguments; never include credentials.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact discovered tool name.",
                    },
                    "arguments_json": {
                        "type": "string",
                        "description": "JSON object matching its schema.",
                    },
                },
                "required": ["name", "arguments_json"],
            }
        ),
    ),
]


@dataclasses.dataclass
class ResearchState:
    client: FlashDataClient
    language: str = "en"


def make_config(state, cfg, msg):
    state.language = msg.get("language") or "en"
    language = gradbot.LANGUAGES.get(state.language, gradbot.LANGUAGES["en"])
    return gradbot.SessionConfig(
        voice_id=msg.get("voice_id") or "cLONiZ4hQ8VpQ4Sz",
        language=language,
        instructions=PROMPT,
        tools=TOOLS,
        **(
            cfg.session_kwargs
            | {
                "rewrite_rules": language.rewrite_rules,
                "assistant_speaks_first": False,
                "silence_timeout_s": 0.0,
            }
        ),
    )


async def on_tool_call(state, handle, input_handle, websocket):
    del input_handle, websocket
    if handle.name == "flashdata_catalog":
        await handle.send_json(await state.client.catalog())
        return
    if handle.name != "flashdata_call":
        await handle.send_error("Unknown research tool.")
        return
    args = handle.args or {}
    try:
        arguments = json.loads(args.get("arguments_json", ""))
    except (TypeError, ValueError):
        await handle.send_error(
            "Provide a JSON object matching the discovered input schema."
        )
        return
    if not isinstance(arguments, dict) or not isinstance(args.get("name"), str):
        await handle.send_error("Provide an exact tool name and a JSON object.")
        return
    await handle.send_json(await state.client.call(args["name"], arguments))
