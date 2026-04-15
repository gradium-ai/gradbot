"""
Three.js Severance Game — Voice AI for clue interactions & Neil check-ins.

Uses gradbot for STT/TTS/LLM voice sessions.
Runs standalone or mounted as a sub-app in the Gradbot demos container.

Standalone:  uvicorn main:app --reload --port 8000
"""

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
import gradbot

from clue_data import CLUES, validate_answer

# ── Logging setup ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("npc_3d_game")

gradbot.init_logging()


# ── Client config ─────────────────────────────────────────────
# Uses config (config.yaml / env vars) — same as other demos.

_cfg = gradbot.config.load(Path(__file__).parent)

VOICES = {
    "Emma": "YTpq7expH9539ERJ",
    "Jack": "m86j6D7UZpGzHsNu",
    "Kent": "LFZvm12tW_z0xfGo",
    "Sydney": "jtEKaLYNn6iif5PR",
    "Eva": "ubuXFxVQwVYnZQhy",
}

log.info("Client kwargs keys: %s", list(_cfg.client_kwargs.keys()))


_CLIENTS = None
_clients_lock = asyncio.Lock()

async def _get_clients():
    """Lazily create clients (lifespan doesn't fire when mounted as sub-app)."""
    global _CLIENTS
    if _CLIENTS is None:
        async with _clients_lock:
            if _CLIENTS is None:
                _CLIENTS = await gradbot.create_clients(**_cfg.client_kwargs)
    return _CLIENTS

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Three.js Game Backend starting...")
    log.info("Three.js Game Backend ready")
    await _get_clients()
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

        log.info("[TTS] Synthesizing directly (no LLM)...")
        clients = await _get_clients()

        voice_id = VOICES.get(voice_name, VOICES["Emma"])
        results = await clients.tts_synthesize(
            text,
            voice_id=voice_id,
            rewrite_rules=gradbot.Lang.En.rewrite_rules,
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

    instructions = f"""You are a mysterious voice inside Gradium Industries — cryptic, enigmatic, and brief.

When you begin, ask the player this question: "{clue['question']}"
Ask it naturally in character. Be cryptic and atmospheric.

RULES:
- After asking the question, listen for the player's answer.
- When the player gives an answer, immediately call check_answer with it.
- If check_answer returns CORRECT: give a brief cryptic congratulation (under 10 words), then stop talking.
- If check_answer returns INCORRECT with retries remaining: give a one-sentence cryptic hint without revealing the answer, then let them try again.
- If check_answer returns MAX_ATTEMPTS_REACHED: say something dismissive like "Perhaps you are not ready" and stop talking.
- NEVER reveal the correct answer under any circumstances.
- Stay in character. Keep ALL responses under 15 words."""

    return gradbot.SessionConfig(
        voice_id=VOICES["Emma"],
        instructions=instructions,
        language=gradbot.Lang.En,
        assistant_speaks_first=True,
        tools=tools,
        **{
            "flush_duration_s": 0.5,
            "rewrite_rules": gradbot.Lang.En.rewrite_rules,
        } | _cfg.session_kwargs,
    )


MAX_CLUE_ATTEMPTS = 3


async def _handle_clue_tool_call(handle, input_handle, websocket, *, clue_id, state):
    """Handle tool calls from the clue voice session."""
    args = handle.args
    if handle.name == "check_answer":
        answer = str(args.get("answer", ""))[:2000]
        correct, fragment = validate_answer(clue_id, answer)
        state["attempts"] += 1
        attempts_exhausted = not correct and state["attempts"] >= MAX_CLUE_ATTEMPTS
        remaining = MAX_CLUE_ATTEMPTS - state["attempts"]
        log.info("[CLUE:%s] Answer submitted (length=%d) -> correct=%s attempts=%d",
                 clue_id, len(answer), correct, state["attempts"])

        if correct:
            tool_msg = f"CORRECT! Truth fragment: {fragment}"
        elif attempts_exhausted:
            tool_msg = "INCORRECT. Maximum attempts reached. Tell the player they are not ready and stop talking."
        else:
            tool_msg = f"INCORRECT. The player has {remaining} attempt(s) left. Give a brief cryptic hint without revealing the answer."

        await handle.send(json.dumps({
            "correct": correct,
            "message": tool_msg,
        }))

        await websocket.send_json({
            "type": "clue_result",
            "correct": correct,
            "fragment": fragment if correct else None,
            "attempts_exhausted": attempts_exhausted,
        })


@app.websocket("/ws/clue/{clue_id}")
async def websocket_clue(websocket: WebSocket, clue_id: str):
    """Voice session for a clue puzzle."""
    if clue_id not in CLUES:
        await websocket.accept()
        await websocket.close(code=4001, reason=f"Unknown clue: {clue_id}")
        return

    state = {"attempts": 0}

    await gradbot.websocket.handle_session(
        websocket,
        on_start=lambda msg: make_clue_session_config(clue_id),
        on_tool_call=lambda h, ih, ws: _handle_clue_tool_call(h, ih, ws, clue_id=clue_id, state=state),
        run_kwargs=_cfg.client_kwargs,
    )


# ── Check-in voice session ──────────────────────────────────

CHECKIN_LINES = [
    "How's the work coming along, Laurent?",
    "Everything alright at your station, Laurent?",
    "Laurent, I noticed you've been away from your desk.",
    "Just checking in. Gradium cares about your wellbeing.",
    "Laurent, are you finding everything you need?",
    "Your department chief asked me to check on you.",
]
_checkin_index = 0


def make_checkin_session_config() -> "gradbot.SessionConfig":
    """Create a SessionConfig for a Neil check-in session."""
    global _checkin_index
    line = CHECKIN_LINES[_checkin_index % len(CHECKIN_LINES)]
    _checkin_index += 1

    instructions = f"""You are Neil (Neil) from Gradium Industries, checking on employee Laurent during his shift.

When you begin, say exactly this: "{line}"

After the player responds, classify their response using the classify_response tool, then stop talking. Do NOT ask follow-up questions.

Classification guide:
- "innocent": normal, work-focused, or appropriately compliant
- "nervous": evasive, hesitant, overly defensive, or mentions anything unusual
- "suspicious": mentions clues, secrets, escape, the outside, or is clearly deceptive

CRITICAL: Call classify_response on the FIRST thing the player says. Say nothing after classifying."""

    tools = [
        gradbot.ToolDef(
            name="classify_response",
            description=(
                "Classify the player's response to your check-in. "
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

    return gradbot.SessionConfig(
        voice_id=VOICES["Jack"],
        instructions=instructions,
        language=gradbot.Lang.En,
        assistant_speaks_first=True,
        tools=tools,
        **{
            "flush_duration_s": 0.5,
            "rewrite_rules": gradbot.Lang.En.rewrite_rules,
        } | _cfg.session_kwargs,
    )


async def _handle_checkin_tool_call(handle, input_handle, websocket):
    """Handle tool calls from the check-in voice session."""
    args = handle.args
    if handle.name == "classify_response":
        valid_classifications = {"innocent", "nervous", "suspicious"}
        classification = args.get("classification", "innocent")
        if classification not in valid_classifications:
            classification = "innocent"
        reason = str(args.get("reason", ""))[:500]
        log.info("[CHECKIN] Classification: %s", classification)

        await handle.send(json.dumps({
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
    """Voice session for Neil check-in."""
    await gradbot.websocket.handle_session(
        websocket,
        on_start=lambda msg: make_checkin_session_config(),
        on_tool_call=_handle_checkin_tool_call,
        run_kwargs=_cfg.client_kwargs,
    )


# ── Static file serving ──────────────────────────────────────
# Serves the Vite build output. When mounted as a sub-app in the
# demos container, all paths are automatically prefixed.

STATIC_DIR = Path(__file__).parent / "static"

gradbot.routes.setup(
    app,
    config=_cfg,
    static_dir=STATIC_DIR if STATIC_DIR.is_dir() else None,
)

if STATIC_DIR.is_dir():
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file = STATIC_DIR / full_path
        if file.is_file() and ".." not in full_path:
            headers = {}
            if full_path.startswith("src/") or full_path.endswith(".js"):
                headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return FileResponse(file, headers=headers)
        return FileResponse(STATIC_DIR / "index.html")

