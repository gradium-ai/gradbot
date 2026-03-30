"""
Three.js Severance Game — Voice AI for clue interactions & Milchick check-ins.

Uses gradbot for STT/TTS/LLM voice sessions.
Runs standalone or mounted as a sub-app in the Gradbot demos container.

Standalone:  uvicorn main:app --reload --port 8000
"""

import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import gradbot
from gradbot.demo_config import load_config, client_config, session_config_overrides, merge_overrides

from clue_data import CLUES, validate_answer

# ── Logging setup ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("threejs_game")

gradbot.init_logging()

# ── Client config ─────────────────────────────────────────────
# Uses demo_config (config.yaml / env vars) — same as other demos.

_YAML_CFG = load_config(Path(__file__).parent)
CLIENT_KWARGS = client_config(_YAML_CFG)
_OVERRIDES = session_config_overrides(_YAML_CFG)

# Allow direct env var overrides (for standalone use / Docker)
def _env_override(kwargs: dict) -> dict:
    if v := os.environ.get("GRADIUM_API_KEY"):
        kwargs["gradium_api_key"] = v
    if v := os.environ.get("GRADIUM_BASE_URL"):
        kwargs["gradium_base_url"] = v
    if v := os.environ.get("LLM_API_KEY"):
        kwargs["llm_api_key"] = v
    if v := os.environ.get("LLM_BASE_URL"):
        kwargs["llm_base_url"] = v
    if v := os.environ.get("LLM_MODEL"):
        kwargs["llm_model_name"] = v
    return kwargs

CLIENT_KWARGS = _env_override(CLIENT_KWARGS)
log.info("Client kwargs keys: %s", list(CLIENT_KWARGS.keys()))


_CLIENTS = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _CLIENTS
    log.info("Three.js Game Backend starting...")
    voices = gradbot.flagship_voices()
    log.info("Available voices: %s", [v.name for v in voices])
    _CLIENTS = await gradbot.create_clients(**CLIENT_KWARGS)
    yield
    log.info("Shutting down...")


app = FastAPI(title="Three.js Severance Game", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse(content={"status": "ok"})


@app.get("/api/clues")
async def list_clues():
    """Return available clue IDs and names (no answers!)."""
    return JSONResponse(content={
        "clues": [
            {"id": k, "name": v["name"]}
            for k, v in CLUES.items()
        ]
    })


# ── TTS endpoint for one-shot speech ─────────────────────────

@app.websocket("/ws/tts")
async def websocket_tts(websocket: WebSocket):
    """
    One-shot TTS: send text, receive OggOpus audio directly (no LLM).

    Protocol:
      Client -> JSON: {"type": "speak", "text": "...", "voice_name": "Emma"}
      Server -> binary OggOpus audio chunks
      Server -> JSON: {"type": "tts_text", "text": "..."}
      Server -> JSON: {"type": "done"}
    """
    await websocket.accept()
    log.info("[TTS] WebSocket connected")

    try:
        msg = await websocket.receive_json()
        if msg.get("type") != "speak":
            await websocket.close(code=4000, reason="Expected speak message")
            return

        text = msg.get("text", "")
        voice_name = msg.get("voice_name", "Emma")
        log.info("[TTS] Speaking: %r with voice %s", text[:80], voice_name)

        voice = gradbot.flagship_voice(voice_name)

        log.info("[TTS] Synthesizing directly (no LLM)...")
        results = await _CLIENTS.tts_synthesize(
            text,
            voice_id=voice.voice_id,
            rewrite_rules=voice.language.rewrite_rules,
        )

        for audio_bytes, tts_text, start_s, stop_s in results:
            if len(audio_bytes) > 0:
                await websocket.send_bytes(audio_bytes)
            if tts_text:
                log.info("[TTS] Text: %r", tts_text)
                await websocket.send_json({
                    "type": "tts_text",
                    "text": tts_text,
                })

        await websocket.send_json({"type": "done"})
        log.info("[TTS] Done")

    except Exception as e:
        log.error("[TTS] Error: %s\n%s", e, traceback.format_exc())
    finally:
        try:
            await websocket.close()
        except:
            pass


# ── Clue voice session ───────────────────────────────────────

def make_clue_session_config(clue_id: str) -> gradbot.SessionConfig:
    """Create a SessionConfig for a clue voice session."""
    clue = CLUES[clue_id]

    tools = [
        gradbot.ToolDef(
            name="check_answer",
            description=(
                "Check if the player's answer to the clue is correct. "
                "Call this whenever the player gives an answer."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The player's answer, as they said it",
                    }
                },
                "required": ["answer"],
            }),
        ),
    ]

    instructions = f"""You are a mysterious voice inside Gradium Industries.

The player has already been asked: "{clue['question']}"
They are about to speak their answer.

CRITICAL RULES:
- Do NOT speak first. Do NOT greet the player. Do NOT re-read the clue. Just listen.
- The FIRST thing the player says IS their answer. Immediately call check_answer with it.
- ANY speech from the player is an answer attempt — call check_answer right away, no matter how short or abbreviated.
- After check_answer returns: if CORRECT, say one short cryptic congratulation. If INCORRECT, say "Think again" or similar — one sentence max.
- Do NOT reveal the correct answer. Do NOT repeat the question.
- Stay in character. Keep responses under 10 words."""

    emma = gradbot.flagship_voice("Emma")
    return gradbot.SessionConfig(
        voice_id=emma.voice_id,
        instructions=instructions,
        language=gradbot.Lang.En,
        assistant_speaks_first=False,
        tools=tools,
        **merge_overrides(_OVERRIDES,
            flush_duration_s=0.5,
            rewrite_rules=gradbot.Lang.En.rewrite_rules,
        ),
    )


async def _handle_clue_tool_call(tool_call, tool_call_handle, input_handle, websocket, *, clue_id):
    """Handle tool calls from the clue voice session."""
    if tool_call.tool_name == "check_answer":
        try:
            args = json.loads(tool_call.args_json)
        except (json.JSONDecodeError, TypeError):
            args = {}
        answer = str(args.get("answer", ""))[:2000]
        correct, fragment = validate_answer(clue_id, answer)
        log.info("[CLUE:%s] Answer submitted (length=%d) -> correct=%s", clue_id, len(answer), correct)

        await tool_call_handle.send(json.dumps({
            "correct": correct,
            "message": (
                f"CORRECT! Truth fragment: {fragment}"
                if correct
                else "INCORRECT. The player should try again."
            ),
        }))

        await websocket.send_json({
            "type": "clue_result",
            "correct": correct,
            "fragment": fragment if correct else None,
        })


@app.websocket("/ws/clue/{clue_id}")
async def websocket_clue(websocket: WebSocket, clue_id: str):
    """Voice session for a clue puzzle."""
    from gradbot.fastapi import websocket_chat_handler

    if clue_id not in CLUES:
        await websocket.accept()
        await websocket.close(code=4001, reason=f"Unknown clue: {clue_id}")
        return

    await websocket_chat_handler(
        websocket,
        on_start=lambda msg: make_clue_session_config(clue_id),
        on_tool_call=lambda tc, tch, ih, ws: _handle_clue_tool_call(tc, tch, ih, ws, clue_id=clue_id),
        run_kwargs=CLIENT_KWARGS,
    )


# ── Check-in voice session ──────────────────────────────────

CHECKIN_SYSTEM_PROMPT = """You are Milchick from Gradium Industries, checking on employee Mark S. during his shift.

The player just responded to your check-in question. Your job is to:
1. Listen to what they say
2. Classify their response using the classify_response tool
3. Give a brief in-character reply (max 15 words)

Classification guide:
- "innocent": The response sounds normal, work-focused, or appropriately compliant
- "nervous": The response sounds evasive, hesitant, overly defensive, or mentions anything unusual
- "suspicious": The response mentions clues, secrets, escape, the outside, innies/outies, or is clearly deceptive

CRITICAL: Call classify_response on the FIRST thing the player says. Do not ask follow-up questions."""


def make_checkin_session_config() -> "gradbot.SessionConfig":
    """Create a SessionConfig for a Milchick check-in session."""
    tools = [
        gradbot.ToolDef(
            name="classify_response",
            description=(
                "Classify the player's response to Milchick's check-in. "
                "Call this as soon as the player speaks."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "classification": {
                        "type": "string",
                        "enum": ["innocent", "nervous", "suspicious"],
                        "description": "How suspicious the player's response is",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for the classification",
                    },
                },
                "required": ["classification"],
            }),
        ),
    ]

    jack = gradbot.flagship_voice("Jack")
    return gradbot.SessionConfig(
        voice_id=jack.voice_id,
        instructions=CHECKIN_SYSTEM_PROMPT,
        language=gradbot.Lang.En,
        assistant_speaks_first=False,
        tools=tools,
        **merge_overrides(_OVERRIDES,
            flush_duration_s=0.5,
            rewrite_rules=gradbot.Lang.En.rewrite_rules,
        ),
    )


async def _handle_checkin_tool_call(tool_call, tool_call_handle, input_handle, websocket):
    """Handle tool calls from the check-in voice session."""
    if tool_call.tool_name == "classify_response":
        try:
            args = json.loads(tool_call.args_json)
        except (json.JSONDecodeError, TypeError):
            args = {}
        valid_classifications = {"innocent", "nervous", "suspicious"}
        classification = args.get("classification", "innocent")
        if classification not in valid_classifications:
            classification = "innocent"
        reason = str(args.get("reason", ""))[:500]
        log.info("[CHECKIN] Classification: %s", classification)

        await tool_call_handle.send(json.dumps({
            "classified": True,
            "message": f"Response classified as {classification}. Give a brief in-character reply.",
        }))

        await websocket.send_json({
            "type": "checkin_result",
            "classification": classification,
            "reason": reason,
        })


@app.websocket("/ws/checkin")
async def websocket_checkin(websocket: WebSocket):
    """Voice session for Milchick check-in."""
    from gradbot.fastapi import websocket_chat_handler

    await websocket_chat_handler(
        websocket,
        on_start=lambda msg: make_checkin_session_config(),
        on_tool_call=_handle_checkin_tool_call,
        run_kwargs=CLIENT_KWARGS,
    )


# ── Static file serving ──────────────────────────────────────
# Serves the Vite build output. When mounted as a sub-app in the
# demos container, all paths are automatically prefixed.

STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.is_dir():
    log.info("Serving static files from %s", STATIC_DIR.resolve())

    # Mount asset directories — these take priority over the SPA catch-all
    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    # Serve audio JS (encoder/decoder workers, audio-processor, etc.) from the
    # gradbot package instead of shipping duplicate copies in static/.
    _bundled_js = Path(gradbot.__file__).parent / "js_audio_processor"
    app.mount("/js/audio", StaticFiles(directory=_bundled_js), name="bundled_js")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve static files or fall back to index.html (SPA)."""
        file = STATIC_DIR / full_path
        if file.is_file() and ".." not in full_path:
            return FileResponse(file)
        return FileResponse(STATIC_DIR / "index.html")
else:
    log.info("No static directory — API-only mode")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
