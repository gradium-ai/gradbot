"""LEGO Voice Assist — the screen IS the guide, the voice is the companion.

Screen-guide edition: the physical booklet leaves the loop. We rasterize every
page of the instruction PDF at parse time and render those pages ON SCREEN, so
the current step is deterministic — no vision reads a booklet, no page-flip
detector, no spread matching. The webcam watches only the messy PILE. The
conversational LLM never sees the camera; it calls tools, and the tools run the
OpenAI vision pipeline: locate this step's known pieces on the labeled 3x3 grid
burned into the pile crop. Grid cells become spoken directions in deterministic
Python (never the model), and the agent relays that sentence verbatim.

The on-screen step and the spoken "next" converge on one server-side position
(state.step_idx). An on-screen prev/next POSTs {step: N} to the frames mailbox;
a voice "next" advances the same index and pushes the new page + highlights back
to the screen. Landing on a step prefetches the FOLLOWING step's pile search, so
the next "next" is a cache hit — one vision call per step, finished before the
current step's speech ends.

Sessions run in English, French, Spanish, German, or Portuguese: a switch_language
tool swaps the voice mid-call, the tools still compose directions in deterministic
English, and the agent translates them faithfully (numbers, colors, and sides are
explicit words, so translation can't mirror them).

Run: uv run uvicorn main:app --reload --port 8410
Needs GRADIUM_API_KEY and OPENAI_API_KEY (reads .env).
"""

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import time

import dotenv
import fastapi
import openai
import pypdfium2 as pdfium
from fastapi import File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

dotenv.load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("lego-voice-assist")

# ── config ───────────────────────────────────────────────────────────────
GRADIUM_API_KEY = os.getenv("GRADIUM_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GRADIUM_API_URL = os.getenv("GRADIUM_API_URL", "https://api.gradium.ai").rstrip("/")
# Default English voice: "Damon" — a bright American voice that lights up with
# the excitement of explaining a favorite obsession. Exactly the energy a LEGO
# building buddy wants. Override with GRADIUM_VOICE_ID (or per-language _FR/etc).
GRADIUM_VOICE_ID = os.getenv("GRADIUM_VOICE_ID", "KUpE0JVhjiIzp1Fk")
# Vision models (Responses API). The heavy calls — manual parsing and pile
# search — default to the flagship; the cheap tier does the first-pass pile
# search and piece checks, escalating to the flagship only when it comes back
# empty or unsure.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-5.6-luna")
# Reasoning effort. Pile search and manual parsing are perception, not logic —
# reading a printed "4x" or spotting a brick barely benefits from reasoning, and
# high effort is where the seconds go (and where responses go `incomplete` and
# trigger the 2× retry). So both default LOW; raise OPENAI_PARSE_EFFORT for a
# careful re-parse if counts come out wrong.
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "high")
OPENAI_FIND_EFFORT = os.getenv("OPENAI_FIND_EFFORT", "low")
OPENAI_PARSE_EFFORT = os.getenv("OPENAI_PARSE_EFFORT", "low")
# STT flush window: after gradbot's ~0.5s pause detection it pushes this much
# silence into STT and waits for it before committing the turn — it is dead
# air between the builder's last word and the agent starting to think. The
# SDK default is 0.5s; 0.3 answers noticeably faster and still finalizes
# reliably. Raise it back if trailing words start getting clipped.
GRADBOT_FLUSH_S = float(os.getenv("GRADBOT_FLUSH_S", "0.3"))

# gradbot reads LLM_* / GRADIUM_BASE_URL from the environment. The
# conversational LLM only routes tool calls and relays results, so it runs on
# the fast tier; override with LLM_MODEL / LLM_EXTRA_CONFIG. Effort must be
# "none": chat completions rejects function tools + reasoning on gpt-5.6-luna
# (and no-reasoning is the right latency call for a voice loop anyway).
if OPENAI_API_KEY:
    os.environ.setdefault("LLM_API_KEY", OPENAI_API_KEY)
os.environ.setdefault("LLM_MODEL", OPENAI_FAST_MODEL)
os.environ.setdefault("GRADIUM_BASE_URL", f"{GRADIUM_API_URL}/api")
LLM_EXTRA_CONFIG = os.getenv("LLM_EXTRA_CONFIG", '{"reasoning_effort": "none"}')

import gradbot  # noqa: E402  (env mapping above must run first)

gradbot.init_logging()  # Rust-side tracing — without it session errors vanish
GRADBOT_CFG = gradbot.config.from_env()

PARSE_CONCURRENCY = int(os.getenv("PARSE_CONCURRENCY", "12"))  # parse fan-out
MAX_PDF_BYTES = 80 * 1024 * 1024
GRID_ROWS, GRID_COLS = 3, 3  # pile cells A1..C3 — must match the frontend grid
FRAMES_FRESH_S = 20.0   # webcam crops older than this are useless (pile moves)
FIND_CACHE_FRESH_S = FRAMES_FRESH_S  # same horizon: a cached find is only as
                                     # trustworthy as the capture it came from
# A find prefetched when a step is shown can live much longer: the pile only
# changes when the builder pulls pieces out, and they only do that AFTER
# hearing where the pieces are. Advancing a step replaces the entry anyway.
PREFETCH_FRESH_S = 180.0
PREFETCH_WAIT_S = 15.0  # a tool would rather join an in-flight prefetch than
                        # start the same capture+vision work from scratch
PILE_SEARCH_TIERED = os.getenv("PILE_SEARCH_TIERED", "1") != "0"
# "high" since the bbox work: at low detail the fast tier misses bricks and
# hallucinates boxes (22% center error measured); at high detail both tiers
# return near-pixel-perfect boxes at no measured latency cost — only tokens.
PILE_IMAGE_DETAIL = os.getenv("PILE_IMAGE_DETAIL", "high")

DATA_DIR = pathlib.Path(__file__).parent / "data"  # parsed-manual cache
DATA_DIR.mkdir(exist_ok=True)

# A 60 s per-call timeout so a wedged upstream fails fast instead of leaving the
# agent stuck on "one moment" — the tool then falls through to a spoken error.
llm = (openai.AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=60.0)
       if OPENAI_API_KEY else None)

app = fastapi.FastAPI(title="LEGO Voice Assist")


def friendly_error(status: int, message: str) -> fastapi.HTTPException:
    return fastapi.HTTPException(status_code=status, detail=message)


# ── vision: pile crop → piece locations ──────────────────────────────────


def _img(b64: str, detail: str = "high") -> dict:
    return {
        "type": "input_image",
        "image_url": f"data:image/jpeg;base64,{b64}",
        "detail": detail,
    }


def _txt(text: str) -> dict:
    return {"type": "input_text", "text": text}


async def _vision_json(system: str, content: list, max_tokens: int = 4000,
                       model: str | None = None, effort: str | None = None,
                       _retry: bool = True) -> dict:
    """One vision → JSON call via the Responses API. max_tokens must budget
    for reasoning tokens too, not just the JSON that comes out — an
    'incomplete' response usually means reasoning ate the budget, so retry
    once with double."""
    if llm is None:
        raise friendly_error(500, "Server is missing OPENAI_API_KEY.")
    try:
        response = await llm.responses.create(
            model=model or OPENAI_MODEL,
            reasoning={"effort": effort or OPENAI_REASONING_EFFORT},
            max_output_tokens=max_tokens,
            text={"format": {"type": "json_object"}},
            # the system prompt rides in `input` (not `instructions`): JSON
            # mode requires the word "json" inside the input messages, and
            # some calls are otherwise image-only
            input=[
                {"role": "developer", "content": [_txt(system)]},
                {"role": "user", "content": content},
            ],
        )
    except openai.OpenAIError as exc:
        log.error("OpenAI error: %s", exc)
        raise friendly_error(502, "Vision lookup failed — try again.")
    if response.status != "completed":
        log.warning("Vision response %s: %s", response.status,
                    getattr(response, "incomplete_details", None))
        if _retry:
            return await _vision_json(system, content, max_tokens * 2,
                                      model, effort, _retry=False)
    text = (response.output_text or "").strip()
    if not text:
        raise friendly_error(502, "Vision lookup came back empty — try again.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.error("Unparseable vision reply: %s", text[:300])
        raise friendly_error(502, "Vision reply wasn't valid JSON — try again.")


FIND_LISTED_PROMPT = """\
You are a LEGO building assistant. You receive ONE photo: the builder's pile
of loose LEGO pieces, with a 3x3 labeled grid drawn on top: rows A-C top to
bottom, columns 1-3 left to right, so cells are A1 (top-left) through C3
(bottom-right).

The user gives you an exact list of pieces to find (already known from the
instruction manual — do not second-guess quantities or colors). For EACH
listed piece, search the pile and report EVERY visible copy — when a piece is
needed 4x and copies sit in different cells, list each one (at most one entry
per needed copy). Each copy gets its grid cell AND a tight bounding box
[x0, y0, x1, y1] normalized to 0.0-1.0 of the full image (x0<x1, y0<y1).
Be honest: an empty list beats a guess, and low confidence beats false
certainty.

Respond with ONLY a JSON object, no markdown fences, in exactly this shape:
{
  "pieces": [
    {
      "description": "<echo the given description>",
      "color": "<echo the given color>",
      "quantity": <echo the given count>,
      "copies": [
        {"cell": "<e.g. 'A1'>", "box": [x0, y0, x1, y1]}
      ],
      "landmark": "<distinctive feature next to the first listed copy,
                    or null>",
      "confidence": <0.0-1.0>
    }
  ],
  "notes": "<one short caveat if something went wrong, else null>"
}

Keep the pieces in the given order. ALL location information belongs in
copies — never put cells, boxes, or directions in notes or any other field,
and never write words like "left" or "top": the app converts locations to
spoken directions itself.\
"""

CHECK_PIECE_PROMPT = """\
You are a LEGO building assistant. The builder has picked up ONE LEGO piece
and is holding it up, asking whether it's the right one.

Image 1 — the full camera frame. The held piece is the one in the builder's
hand or fingers, usually in the foreground and larger than anything lying on
the desk. Ignore every piece that is lying in the pile or on the desk — judge
ONLY the piece being held. The pieces the current step needs are in the user
message.

Decide one verdict:
- "match": the held piece clearly matches one needed piece — same shape AND
  same color. Judge shape by stud count and proportions (a 2x2 is square, a
  2x4 is twice as long; a plate is thin, a brick is tall).
- "different": a held piece is clearly visible but matches no needed piece
  (wrong shape or wrong color — light gray and dark gray are different).
- "unsure": nothing is held up, or it's too blurry, too small, or too hidden
  to judge. Be honest — "unsure" beats a guess, every time.

Respond with ONLY a JSON object, no markdown fences:
{
  "verdict": "match" | "different" | "unsure",
  "holding": {"description": "<shape, e.g. '2x4 brick'>",
              "color": "<color name>"} or null,
  "match_index": <0-based index into the needed-pieces list, or null>,
  "confidence": <0.0-1.0>,
  "notes": "<one short caveat, or null>"
}

Keep descriptions short and TTS-friendly. Never use direction words like
left, right, or top — the app never speaks positions from this check.\
"""

MANUAL_PAGE_PROMPT = """\
You see one page of a LEGO instruction manual (a clean PDF render).

Respond with ONLY a JSON object, no markdown fences:
{
  "page": <the printed page number on this page, or null if none>,
  "steps": [
    {
      "step": <step number>,
      "pieces": [
        {"description": "<shape, e.g. '2x4 brick'>",
         "color": "<color name>",
         "quantity": <count from the "Nx" multiplier>}
      ]
    }
  ]
}

Rules: pieces come ONLY from each step's parts callout (the boxed list at the
top of the step with counts like "2x"). Read every count multiplier exactly.
Include sub-assembly callouts with their parent step. Pages with no build
steps (covers, inventory pages, ads) get "steps": []. Keep descriptions short
and TTS-friendly.\
"""


def _b64_payload(data_url: str, field: str) -> str:
    """Accept 'data:image/jpeg;base64,...' or bare base64; return bare base64."""
    payload = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    try:
        base64.b64decode(payload[:64], validate=True)
    except (binascii.Error, ValueError):
        raise friendly_error(400, f"'{field}' is not a base64 image.")
    return payload


def _needs_escalation(result: dict) -> bool:
    """An empty copies list beats a guess — but a piece the manual says this
    step NEEDS should be in the pile, so any empty is worth one sharper look
    on the flagship before settling for 'I couldn't spot it.' (The fast tier
    misses obvious bricks while reporting high confidence — a confident empty
    is not evidence of absence.) On the prefetch path the extra call runs in
    the background, so it costs the builder nothing."""
    return any(not (p.get("copies") or p.get("grid_cells"))
               for p in result.get("pieces", []))


async def _find_listed(pile_b64: str, pieces: list[dict],
                       escalate: bool = True) -> dict:
    """Locate an already-known list of pieces in the pile crop. Tries the cheap
    tier first (low effort, smaller token budget). `escalate=True` (the
    background/prefetch path) then re-runs on the flagship when a piece comes
    back empty — on a dense real pile the fast tier misses SOMETHING almost
    every time, so escalation is the norm, not the exception, and it roughly
    doubles the latency. That is fine when it's hidden in a prefetch, so the
    LIVE ask path passes `escalate=False`: one fast call, answer now, and the
    look-ahead prefetch supplies the accurate version for next time.
    Set PILE_SEARCH_TIERED=0 to always use the flagship, as before."""
    wanted = "\n".join(
        f"- {p.get('quantity', 1)}x {p.get('color', '')} {p.get('description', 'piece')}".strip()
        for p in pieces
    )

    async def attempt(model: str, effort: str, detail: str, max_tokens: int = 4000) -> dict:
        result = await _vision_json(FIND_LISTED_PROMPT, [
            _img(pile_b64, detail=detail),
            _txt(f"Find these pieces in my pile:\n{wanted}"),
        ], max_tokens=max_tokens, model=model, effort=effort)
        result.setdefault("pieces", [])
        return result

    if PILE_SEARCH_TIERED:
        result = await attempt(OPENAI_FAST_MODEL, "low", PILE_IMAGE_DETAIL, max_tokens=1800)
        if escalate and _needs_escalation(result):
            log.info("pile search low-confidence on %s — escalating to %s",
                     OPENAI_FAST_MODEL, OPENAI_MODEL)
            result = await attempt(OPENAI_MODEL, OPENAI_FIND_EFFORT, "high")
    else:
        result = await attempt(OPENAI_MODEL, OPENAI_FIND_EFFORT, "high")
    result.setdefault("notes", None)
    return result


def _piece_name(p: dict) -> str:
    """'red 2 by 4 brick' — color folded in unless already there (mirrors the
    guard in _build_speech; cheap tiers love pre-folding the color)."""
    color = (p.get("color") or "").strip()
    desc = (p.get("description") or "piece").strip()
    if color and desc.lower().startswith(color.lower() + " "):
        return _speakable(desc)
    return _speakable(" ".join(x for x in [color, desc] if x))


async def _check_piece(scene_b64: str, pieces: list[dict]) -> dict:
    """Is the piece the builder is holding one of the step's pieces? The needed
    list (from the parsed manual) rides in the text. Tiered fast→flagship like
    the pile search, but escalation triggers on an unsure/low-confidence
    verdict — a wrong yes/no here is worse than an extra second of latency."""
    wanted = "\n".join(
        f"{i}: {p.get('quantity', 1)}x {_piece_name(p)}"
        for i, p in enumerate(pieces))
    text = f"The step needs these pieces:\n{wanted}\nWhat am I holding?"

    async def attempt(model: str, effort: str, max_tokens: int = 2500) -> dict:
        result = await _vision_json(CHECK_PIECE_PROMPT, [_img(scene_b64), _txt(text)],
                                    max_tokens=max_tokens, model=model,
                                    effort=effort)
        if result.get("verdict") not in ("match", "different", "unsure"):
            result["verdict"] = "unsure"
        return result

    # MEDIUM effort on the fast tier, not low: judging a single held brick is a
    # decision the low tier punts on ("unsure") almost every time, which used to
    # force the flagship escalation on EVERY check (~12 s). At medium the fast
    # tier commits — a wrong brick comes back "different" at ~3 s, one call.
    result = await attempt(OPENAI_FAST_MODEL, "medium", max_tokens=1500)
    conf = result.get("confidence") or 0
    # Escalate only when a fast answer would still be UNSAFE: it genuinely can't
    # tell (unsure), or it's a low-confidence "match" — a false "yes, that's
    # right" is the worst failure this feature has. A "different" verdict returns
    # immediately, and _check_speech already hedges a low-confidence match.
    if result["verdict"] == "unsure" or (result["verdict"] == "match" and conf < 0.6):
        log.info("check_piece low-confidence on %s — escalating to %s",
                 OPENAI_FAST_MODEL, OPENAI_MODEL)
        result = await attempt(OPENAI_MODEL, OPENAI_FIND_EFFORT)
    result.setdefault("notes", None)
    return result


# ── egocentric directions: grid cell → "close to you, on your right" ─────
# The vision model reports image-space cells; the flip to YOUR left/right and
# near/far depends only on where the camera sits, so it's exact code here —
# never left to a model. This used to live in the browser; the gradbot tools
# need it server-side, and it must exist in exactly one place.

NUM_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
             "eight", "nine", "ten"]


def _num_word(n: int) -> str:
    return NUM_WORDS[n] if 0 <= n < len(NUM_WORDS) else str(n)


def _speakable(text) -> str:
    # "2x4 brick" reads terribly as-is over TTS
    return re.sub(r"(\d+)\s*[x×]\s*(\d+)", r"\1 by \2", str(text), flags=re.I)


def _parse_cell(cell) -> tuple[int, int] | None:
    m = re.match(r"^([A-Z])(\d+)$", str(cell or "").upper().strip())
    if not m:
        return None
    row, col = ord(m.group(1)) - 65, int(m.group(2)) - 1
    if 0 <= row < GRID_ROWS and 0 <= col < GRID_COLS:
        return row, col
    return None


def _parse_box(box) -> list[float] | None:
    """Tight bounding box [x0,y0,x1,y1], normalized. Rejects malformed or
    degenerate boxes; the cell stays the fallback highlight."""
    if not (isinstance(box, list) and len(box) == 4):
        return None
    try:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    if x1 - x0 < 0.005 or y1 - y0 < 0.005:
        return None
    return [x0, y0, x1, y1]


def _piece_cells(p: dict) -> list[dict]:
    """One entry per visible copy (a 4x piece can be scattered): the new
    copies shape carries {cell, box}; tolerate the older grid_cells /
    grid_cell shapes too. A box only survives if its centroid lands in the
    reported cell — measured failure mode: when a model botches the box, the
    cell is still right, so the mismatch gate drops exactly the bad boxes and
    the UI falls back to the cell highlight (speech never uses boxes)."""
    raw = p.get("copies")
    if not isinstance(raw, list):
        cells = p.get("grid_cells")
        if not isinstance(cells, list):
            cells = [p["grid_cell"]] if p.get("grid_cell") else []
        raw = [{"cell": c} for c in cells]
    out = []
    for c in raw:
        if not isinstance(c, dict):
            c = {"cell": c}
        at = _parse_cell(c.get("cell"))
        if not at:
            continue
        entry = {"row": at[0], "col": at[1],
                 "label": str(c["cell"]).upper().strip()}
        box = _parse_box(c.get("box"))
        if box:
            mid_row = min(GRID_ROWS - 1, int((box[1] + box[3]) / 2 * GRID_ROWS))
            mid_col = min(GRID_COLS - 1, int((box[0] + box[2]) / 2 * GRID_COLS))
            if (mid_row, mid_col) == at:
                entry["box"] = box
        out.append(entry)
    return out


def _egocentric_phrase(row: int, col: int, orientation: str) -> str:
    # Webcam facing you: distant desk sits higher in frame, so image-top is
    # YOUR edge of the pile, and image-left is your right. Over-shoulder: the
    # image matches your own view.
    facing = orientation != "overhead"
    depth = row if facing else (GRID_ROWS - 1) - row          # 0 = near you
    side = (GRID_COLS - 1) - col if facing else col           # 0 = your left
    if side == 1:
        return ["right in front of you", "dead center of the pile",
                "straight ahead at the far side"][depth]
    depth_name = ["close to you", "in the middle", "at the far side"][depth]
    side_name = ["on your left", "", "on your right"][side]
    return f"{depth_name}, {side_name}"


def _location_phrase(cells: list[dict], orientation: str) -> str:
    """"two in the middle, one close to you on your left" — copies grouped by
    their spoken direction, capped so speech stays speech-sized."""
    groups: list[dict] = []
    for c in cells:
        phrase = _egocentric_phrase(c["row"], c["col"], orientation)
        for g in groups:
            if g["phrase"] == phrase:
                g["n"] += 1
                break
        else:
            groups.append({"phrase": phrase, "n": 1})
    if len(cells) == 1:
        return groups[0]["phrase"]
    parts = [f"{_num_word(g['n'])} {g['phrase']}" for g in groups[:3]]
    if len(groups) > 3:
        parts.append("and more elsewhere")
    # semicolons: the direction phrases contain commas themselves, and the TTS
    # pause keeps copy boundaries audible
    return "; ".join(parts)


def _build_speech(result: dict, step: dict | None, orientation: str) -> str:
    pieces = result.get("pieces") or []
    prefix = f"Step {step['step']}: " if step else ""
    if not pieces:
        return prefix + _speakable(
            result.get("notes") or "I couldn't read a parts list for that step.")
    lines = []
    for p in pieces:
        qty = p.get("quantity") or 1
        color = (p.get("color") or "").strip()
        desc = (p.get("description") or "piece").strip()
        # Prompts ask the model to echo color and description separately so
        # we can safely prepend color here — but compliance varies by model,
        # and a cheaper tier is more prone to folding the color into the
        # description on its own ("black 4x4 plate" + color "black"). Don't
        # double-speak it if it's already there.
        if color and desc.lower().startswith(color.lower() + " "):
            name_bits = desc
        else:
            name_bits = " ".join(x for x in [color, desc] if x)
        name = f"{_num_word(qty)} {_speakable(name_bits)}{'s' if qty > 1 else ''}"
        cells = _piece_cells(p)
        if not cells:
            lines.append(f"{name} — I couldn't spot "
                         f"{'those' if qty > 1 else 'that one'}")
            continue
        conf = p.get("confidence")
        maybe = "probably " if (conf if conf is not None else 1) < 0.4 else ""
        near = (f", next to the {_speakable(p['landmark'])}"
                if len(cells) == 1 and p.get("landmark") else "")
        lines.append(f"{name} — {maybe}"
                     f"{_location_phrase(cells, orientation)}{near}")
    text = f"{prefix}You need {'. Also '.join(lines)}."
    if result.get("notes"):
        text += f" {_speakable(result['notes'])}"
    return text


def _compose(result: dict, step: dict | None, orientation: str) -> dict:
    """Enrich a raw vision result with parsed cells, per-piece location
    phrases, and the full spoken summary — everything the UI and the voice
    agent need, composed in one deterministic place."""
    pieces = []
    for p in result.get("pieces") or []:
        cells = _piece_cells(p)
        pieces.append({
            **p,
            "cells": cells,
            "phrase": _location_phrase(cells, orientation) if cells else None,
        })
    return {**result, "pieces": pieces,
            "summary": _build_speech(result, step, orientation)}


def _check_speech(result: dict, pieces: list[dict] | None) -> str:
    """Verdict JSON → one honest spoken sentence. Deterministic on purpose:
    a fabricated 'yes, that's right' is the worst failure this feature has."""
    verdict = result.get("verdict")
    holding = result.get("holding") or {}
    hold_name = _piece_name(holding) if holding.get("description") else None
    conf = result.get("confidence")
    low_conf = (conf if conf is not None else 1) < 0.6

    if verdict == "match":
        mi = result.get("match_index")
        target = (pieces[mi] if pieces and isinstance(mi, int)
                  and 0 <= mi < len(pieces) else None)
        name = _piece_name(target) if target else (hold_name or "right piece")
        say = (f"That looks like the {name} to me."
               if low_conf else f"Yes — that's the {name}.")
        qty = (target or {}).get("quantity") or 1
        if qty > 1:
            say += f" You need {_num_word(qty)} of those in total."
        return say

    if verdict == "different":
        if pieces:
            needed = " or the ".join(_piece_name(p) for p in pieces[:3])
            wrong = (f"that looks like a {hold_name}, " if hold_name else "")
            return (f"Hmm, {wrong}not one of this step's pieces — "
                    f"you're after the {needed}.")
        return (f"That looks like a {hold_name}, but it doesn't match what "
                f"this step needs." if hold_name else
                "That doesn't look like one of this step's pieces.")

    return ("I can't see it well enough — hold it a bit closer to the "
            "camera, nice and steady, and ask me again.")


# ── REST lookup (backend smoke tests) ────────────────────────────────────


class FindRequest(BaseModel):
    pile: str  # JPEG as data URL or bare base64
    pieces: list[dict]  # known list from the parsed manual
    orientation: str | None = None  # "facing"/"overhead" → composed phrases


@app.post("/api/find")
async def find_pieces(req: FindRequest) -> JSONResponse:
    pile_b64 = _b64_payload(req.pile, "pile")
    if not req.pieces:
        raise friendly_error(400, "Need a non-empty 'pieces' list to search for.")
    result = await _find_listed(pile_b64, req.pieces)
    if req.orientation in ("facing", "overhead"):
        result = _compose(result, None, req.orientation)
    return JSONResponse(result)


# ── manual PDF: parse once, pages render on screen, quantities authoritative ─
# In-memory registry of parses; finished manuals also persist to data/ keyed
# by content hash (parsed steps as <id>.json, page renders as <id>/page-NNN.jpg),
# so re-uploading the same PDF is instant and the guide has images to show.
MANUALS: dict[str, dict] = {}


def _pages_dir(manual_id: str) -> pathlib.Path:
    return DATA_DIR / manual_id


def _render_all_pages(pdf_bytes: bytes, out_dir: pathlib.Path) -> list[str]:
    """PDF → base64 JPEG per page, persisting each render to out_dir as
    page-NNN.jpg so the on-screen guide can serve it later. PDFium isn't
    thread-safe per document, so render sequentially in one worker thread; the
    vision calls fan out after."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    out = []
    for i, page in enumerate(pdf):
        w, h = page.get_size()  # points
        scale = min(3.0, 1600 / max(w, h))
        pil = page.render(scale=scale).to_pil().convert("RGB")
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
        (out_dir / f"page-{i:03d}.jpg").write_bytes(data)
        out.append(base64.b64encode(data).decode())
    return out


def _infer_missing_page_numbers(pages: list[dict]) -> None:
    """Fill unprinted page numbers from numbered neighbours (booklets number
    nearly every page; covers often aren't)."""
    for i in range(1, len(pages)):
        if pages[i]["page"] is None and pages[i - 1]["page"] is not None:
            pages[i]["page"] = pages[i - 1]["page"] + 1
    for i in range(len(pages) - 2, -1, -1):
        if pages[i]["page"] is None and pages[i + 1]["page"] is not None:
            pages[i]["page"] = pages[i + 1]["page"] - 1


async def _parse_manual(manual_id: str, pdf_bytes: bytes) -> None:
    entry = MANUALS[manual_id]
    try:
        images = await asyncio.to_thread(
            _render_all_pages, pdf_bytes, _pages_dir(manual_id))
        entry["total"] = len(images)
        entry["pages"] = [None] * len(images)
        sem = asyncio.Semaphore(PARSE_CONCURRENCY)

        async def one(i: int, b64: str) -> None:
            async with sem:
                try:
                    data = await _vision_json(MANUAL_PAGE_PROMPT, [_img(b64)],
                                              max_tokens=4000,
                                              effort=OPENAI_PARSE_EFFORT)
                except Exception as exc:
                    log.warning("manual %s page %d: %s", manual_id, i + 1, exc)
                    data = {}
                entry["pages"][i] = {
                    "page": data.get("page") if isinstance(data.get("page"), int) else None,
                    "steps": data.get("steps") or [],
                }
                entry["done"] += 1

        await asyncio.gather(*(one(i, b64) for i, b64 in enumerate(images)))
        _infer_missing_page_numbers(entry["pages"])
        entry["status"] = "done"
        (DATA_DIR / f"{manual_id}.json").write_text(
            json.dumps({"pages": entry["pages"]})
        )
        log.info("manual %s parsed: %d pages", manual_id, entry["total"])
    except Exception as exc:
        log.error("manual %s parse failed: %s", manual_id, exc)
        entry["status"] = "error"
        entry["error"] = "Parsing failed — check the server log."


@app.post("/api/manual")
async def upload_manual(pdf: UploadFile = File(...)) -> JSONResponse:
    if llm is None:
        raise friendly_error(500, "Server is missing OPENAI_API_KEY.")
    data = await pdf.read()
    if not data.startswith(b"%PDF"):
        raise friendly_error(400, "That doesn't look like a PDF.")
    if len(data) > MAX_PDF_BYTES:
        raise friendly_error(400, "PDF is too large.")
    manual_id = hashlib.sha256(data).hexdigest()[:12]
    cached = DATA_DIR / f"{manual_id}.json"
    if manual_id not in MANUALS and cached.exists():
        pages = json.loads(cached.read_text())["pages"]
        MANUALS[manual_id] = {"status": "done", "done": len(pages),
                              "total": len(pages), "pages": pages}
    if manual_id not in MANUALS:
        MANUALS[manual_id] = {"status": "parsing", "done": 0, "total": 0, "pages": []}
        asyncio.get_running_loop().create_task(_parse_manual(manual_id, data))
    entry = MANUALS[manual_id]
    return JSONResponse({"id": manual_id, "status": entry["status"],
                         "done": entry["done"], "total": entry["total"]})


def _manual_entry(manual_id: str) -> dict | None:
    """The parse registry entry for a manual, hydrating from the on-disk cache
    on a cold process."""
    entry = MANUALS.get(manual_id)
    if entry is None:
        cached = DATA_DIR / f"{manual_id}.json"
        if not cached.exists():
            return None
        pages = json.loads(cached.read_text())["pages"]
        entry = MANUALS[manual_id] = {"status": "done", "done": len(pages),
                                      "total": len(pages), "pages": pages}
    return entry


@app.get("/api/manual/{manual_id}")
async def manual_status(manual_id: str) -> JSONResponse:
    entry = _manual_entry(manual_id)
    if entry is None:
        raise friendly_error(404, "No such manual.")
    body = {"id": manual_id, "status": entry["status"],
            "done": entry["done"], "total": entry["total"]}
    if entry["status"] == "done":
        body["pages"] = entry["pages"]
        # which physical page render backs each page, so the guide can show it
        body["has_images"] = _pages_dir(manual_id).is_dir()
    if entry["status"] == "error":
        body["error"] = entry.get("error")
    return JSONResponse(body)


@app.get("/api/manual/{manual_id}/page/{n}")
async def manual_page_image(manual_id: str, n: int) -> FileResponse:
    """A rasterized manual page — the on-screen guide's image for a step."""
    path = _pages_dir(manual_id) / f"page-{n:03d}.jpg"
    if not path.exists():
        raise friendly_error(404, "No render for that page.")
    return FileResponse(path, media_type="image/jpeg")


def _load_flat_steps(manual_id: str | None) -> list[dict]:
    """Steps in book order for a finished manual: [{page, step, pieces, img}].
    `img` is the physical page index (which page-NNN.jpg render shows the step).
    Steps with an empty parts list are dropped (nothing to find), matching what
    the frontend refuses to adopt."""
    if not manual_id:
        return []
    entry = _manual_entry(manual_id)
    if entry is None or entry.get("status") != "done":
        return []
    return [
        {"page": pg.get("page"), "step": s.get("step"),
         "pieces": s["pieces"], "img": i}
        for i, pg in enumerate(entry["pages"] or [])
        for s in (pg or {}).get("steps") or []
        if s.get("pieces")
    ]


# ── voice sessions: gradbot loop + per-session state ──────────────────────
# The browser opens /ws/chat, streams mic audio, and plays streamed TTS back;
# gradbot multiplexes STT, the conversational LLM, and TTS with turn-taking
# and barge-in. State lives here so the tools can use it.


@dataclasses.dataclass
class Session:
    sid: str
    orientation: str = "facing"
    lang: str = "en"                  # session language code (en/fr/es/de/pt)
    manual_id: str | None = None
    regions_ready: bool = False       # camera on + pile framed
    websocket: fastapi.WebSocket | None = None
    frames: dict | None = None        # {"pile","scene": b64|None, "ts": monotonic}
    frames_event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    flat_steps: list = dataclasses.field(default_factory=list)
    step_idx: int = -1                # current position in flat_steps
    prefetch_task: asyncio.Task | None = None  # in-flight step prefetch
    warm_tasks: set = dataclasses.field(default_factory=set)  # look-ahead finds
    find_cache: dict = dataclasses.field(default_factory=dict)
    # {("step", flat_idx) | ("adhoc", description):
    #      (found_at, ttl, summary, payload)}
    # A repeat ask within ttl skips the capture round-trip and vision call.
    # Step keys use the index into flat_steps, not the printed step number —
    # numbering restarts across sub-builds, and prefetched entries now live
    # long enough (PREFETCH_FRESH_S) for a collision to actually bite.


SESSIONS: dict[str, Session] = {}

NO_PILE_SAY = ("I can't see your pile. Make sure the camera is on and pointed "
               "at your pile of pieces.")
NO_SCENE_SAY = ("I can't get a picture from the camera to check it. Make sure "
                "the camera is on, then hold the piece up and ask again.")


class FramesIn(BaseModel):
    pile: str | None = None
    scene: str | None = None  # full camera frame — check_piece looks here
    orientation: str | None = None
    warm: bool = False        # camera just became ready → prefetch current step
    step: int | None = None   # on-screen prev/next → set current step + prefetch


@app.post("/api/frames/{sid}")
async def post_frames(sid: str, req: FramesIn) -> JSONResponse:
    """The browser's answer to a {"type": "capture"} event — and its own
    unprompted push when the camera becomes ready (warm) or the builder taps
    the on-screen prev/next control (step)."""
    state = SESSIONS.get(sid)
    if state is None:
        raise friendly_error(404, "No live voice session with that id.")
    if req.orientation in ("facing", "overhead"):
        state.orientation = req.orientation
    if req.pile or req.scene:
        state.frames = {
            "pile": _b64_payload(req.pile, "pile") if req.pile else None,
            "scene": _b64_payload(req.scene, "scene") if req.scene else None,
            "ts": time.monotonic(),
        }
    state.frames_event.set()
    target = None
    if req.step is not None and state.flat_steps:
        state.step_idx = max(0, min(req.step, len(state.flat_steps) - 1))
        target = state.step_idx
    elif req.warm and state.flat_steps:
        target = max(state.step_idx, 0)
    if target is not None and state.frames and state.frames.get("pile"):
        _start_prefetch(state, target)
    return JSONResponse({"ok": True})


def _start_prefetch(state: Session, step_idx: int) -> None:
    """Kick off (or restart) the background prefetch for an explicit step. One
    in flight per session: a newer step supersedes whatever the old one was
    computing."""
    if state.prefetch_task and not state.prefetch_task.done():
        state.prefetch_task.cancel()
    state.prefetch_task = asyncio.get_running_loop().create_task(
        _prefetch(state, step_idx))


async def _await_prefetch(state: Session) -> None:
    """Join the in-flight push-prefetch (the one that renders highlights on a
    warm-up or on-screen step change) rather than racing it. Bounded by
    PREFETCH_WAIT_S. NOTE: this waits ONLY on state.prefetch_task, never on the
    background look-ahead finds (state.warm_tasks) — those run the slow
    escalated path and, on a dense pile, can take 20 s+. Blocking a live ask on
    them made "what about this page" hang even when the current step was already
    cached. The look-aheads still populate the cache opportunistically; a miss
    just falls through to the fast single-tier live path."""
    task = state.prefetch_task
    if not task or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=PREFETCH_WAIT_S)
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # OUR task got cancelled, not the prefetch — propagate
    except Exception:
        pass  # timeout or failed prefetch — fall through to the live path


def _cache_find(state: Session, key: tuple, say: str, payload: dict,
                ttl: float) -> None:
    state.find_cache[key] = (time.monotonic(), ttl, say, payload)


def _is_hot(state: Session, key: tuple) -> bool:
    """Is this answer already cached and fresh? A non-logging peek used to skip
    the prefetch wait entirely when the asked step is ready — the whole point of
    the on-flip highlights is that asking about the current page is instant."""
    hit = state.find_cache.get(key)
    return bool(hit and time.monotonic() - hit[0] <= hit[1])


def _cached_find(state: Session, key: tuple) -> tuple[str, dict] | None:
    hit = state.find_cache.get(key)
    if not hit:
        return None
    found_at, ttl, say, payload = hit
    age = time.monotonic() - found_at
    if age > ttl:
        return None
    log.info("find cache HIT %s (age %.0fs)", key, age)
    return say, payload


def _composed_step_payload(state: Session, result: dict, idx: int) -> tuple[str, dict]:
    """Vision result → (say, UI payload) for a manual step — shared by the
    live path and the prefetcher so both compose identically. The payload
    carries which page render the guide should show."""
    step = state.flat_steps[idx]
    composed = _compose(result, step, state.orientation)
    image_url = (f"api/manual/{state.manual_id}/page/{step['img']}"
                 if state.manual_id is not None else None)
    payload = {"type": "find_result", "summary": composed["summary"],
               "pieces": composed["pieces"], "notes": composed.get("notes"),
               "step": {"step": step["step"], "page": step["page"],
                        "index": idx + 1, "total": len(state.flat_steps),
                        "img": step["img"], "image_url": image_url}}
    return composed["summary"], payload


async def _step_find(state: Session, idx: int, push: bool,
                     escalate: bool = True) -> tuple[str, dict] | None:
    """Compose the answer for a manual step from the current pile frame, cache
    it under ("step", idx), and optionally push it to the UI. Reuses a fresh
    cache entry instead of re-running vision. Background use escalates for
    accuracy (its latency is hidden); returns None when there is no pile frame
    to work from."""
    key = ("step", idx)
    cached = _cached_find(state, key)
    if cached:
        say, payload = cached
    else:
        pile = (state.frames or {}).get("pile")
        if not pile:
            return None
        result = await _find_listed(pile, state.flat_steps[idx]["pieces"],
                                    escalate=escalate)
        say, payload = _composed_step_payload(state, result, idx)
        _cache_find(state, key, say, payload, PREFETCH_FRESH_S)
    if push:
        await _push_step(state, payload)
    return say, payload


def _warm_ahead(state: Session, idx: int) -> None:
    """Fire-and-forget: pre-compute the following step's find on the current
    pile frame, so a voice 'next' lands as a cache hit. The pile hasn't moved
    (the builder pulls pieces only after hearing where they are), so the old
    frame is the right one to reuse."""
    if not (0 <= idx < len(state.flat_steps)) or _cached_find(state, ("step", idx)):
        return
    task = asyncio.get_running_loop().create_task(_step_find(state, idx, push=False))
    state.warm_tasks.add(task)
    task.add_done_callback(state.warm_tasks.discard)


async def _prefetch(state: Session, idx: int) -> None:
    """The latency play, now trivially exact: the step is known, so there is no
    guessing and no spread read — one pile search composes the whole answer,
    caches it, and pushes the highlights to the screen before the builder even
    asks. Then look one step ahead so the next 'next' is a cache hit too.

    The agent still can't speak unprompted — the prefetched answer surfaces as
    on-screen highlights plus a status hint, and the spoken answer waits for
    the builder's 'next' (or 'where are my pieces')."""
    t0 = time.monotonic()
    try:
        # advancing steps means the pile is about to change — one-piece answers
        # from the old frame are no longer trustworthy
        state.find_cache = {k: v for k, v in state.find_cache.items()
                            if k[0] != "adhoc"}
        if not state.flat_steps:
            return
        result = await _step_find(state, idx, push=True)
        if result is None:
            await _push_hint(state, "Point the camera at your pile and I'll "
                                    "find the pieces.")
            return
        _warm_ahead(state, idx + 1)
        log.info("prefetch step=%s done in %.1fs", idx, time.monotonic() - t0)
    except asyncio.CancelledError:
        raise  # superseded by a newer step
    except Exception as exc:
        log.warning("prefetch: %s", exc)


async def _push_step(state: Session, payload: dict) -> None:
    """Show the builder the current step: guide page + grid highlights render
    before they even ask, and a status hint tells them the answer is hot."""
    if state.websocket is None:
        return
    step = payload.get("step") or {}
    hint = f"Step {step.get('step')} ready." if step else "Ready."
    try:
        await state.websocket.send_json(payload)
        await state.websocket.send_json({"type": "status", "hint": hint})
    except Exception:
        pass  # UI update is best-effort


async def _push_hint(state: Session, hint: str) -> None:
    if state.websocket is None:
        return
    try:
        await state.websocket.send_json({"type": "status", "hint": hint})
    except Exception:
        pass


async def _fresh_frames(state: Session) -> dict | None:
    """Ask the browser for a just-captured pile crop; fall back to a recent one
    or give up."""
    if state.websocket is None:
        return None
    state.frames_event.clear()
    try:
        await state.websocket.send_json({"type": "capture"})
        await asyncio.wait_for(state.frames_event.wait(), timeout=6.0)
    except (asyncio.TimeoutError, Exception):  # noqa: B014 — ws may be gone
        pass
    frames = state.frames
    if frames and time.monotonic() - frames["ts"] <= FRAMES_FRESH_S:
        return frames
    return None


async def _locate(state: Session, websocket: fastapi.WebSocket,
                  step_idx: int | None = None, adhoc: dict | None = None) -> str:
    """Grab a fresh pile crop, run the search, push the visual result to the
    browser (guide page + grid highlights), and return the one sentence the
    agent should say. Cache first: a prefetch has usually composed the answer
    already, and a repeat ask about the same step/piece reuses the last answer
    instead of re-capturing and re-running vision."""
    t0 = time.monotonic()
    cache_key = (("step", step_idx) if step_idx is not None
                 else ("adhoc", adhoc["description"].strip().lower()))
    cached = _cached_find(state, cache_key)
    if cached:
        say, payload = cached
        try:
            await websocket.send_json(payload)
        except Exception:
            pass
        return say

    frames = await _fresh_frames(state)
    if not frames or not frames.get("pile"):
        return NO_PILE_SAY
    # LIVE path = single fast-tier call (escalate=False): the prefetch usually
    # beat us here anyway; when it didn't, a fast answer now beats a perfect one
    # in twelve seconds. The accurate (escalated) version is what the background
    # look-ahead composes for the NEXT step, so the common "next" stays sharp.
    if adhoc is not None:
        result = await _find_listed(frames["pile"], [adhoc], escalate=False)
        composed = _compose(result, None, state.orientation)
        payload = {"type": "find_result", "summary": composed["summary"],
                   "pieces": composed["pieces"], "notes": composed.get("notes")}
        say = composed["summary"]
    else:
        result = await _find_listed(frames["pile"],
                                    state.flat_steps[step_idx]["pieces"],
                                    escalate=False)
        say, payload = _composed_step_payload(state, result, step_idx)
    try:
        await websocket.send_json(payload)
    except Exception:  # UI update is best-effort; the spoken answer matters
        pass
    _cache_find(state, cache_key, say, payload, FIND_CACHE_FRESH_S)
    if step_idx is not None:
        _warm_ahead(state, step_idx + 1)  # keep the next "next" instant
    log.info("find cache MISS %s — live lookup took %.1fs",
             cache_key, time.monotonic() - t0)
    return say


def _to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


NO_MANUAL_SAY = ("Load your set's instructions first — upload the PDF on the "
                 "screen and I'll walk you through it.")


async def _tool_next_step(state: Session, websocket) -> str:
    if not state.flat_steps:
        return NO_MANUAL_SAY
    if state.step_idx >= len(state.flat_steps) - 1:
        return "That was the last step I have parts for. Nice build!"
    idx = 0 if state.step_idx < 0 else state.step_idx + 1
    state.step_idx = idx
    # If the look-ahead already cached this step, answer instantly — don't wait.
    # Only join an in-flight push-prefetch when the answer isn't ready yet.
    if not _is_hot(state, ("step", idx)):
        await _await_prefetch(state)
    return await _locate(state, websocket, step_idx=idx)


async def _tool_find_step(state: Session, websocket,
                          step_number, page_number) -> str:
    if not state.flat_steps:
        return NO_MANUAL_SAY
    sn, pn = _to_int(step_number), _to_int(page_number)
    if sn is not None:
        idx = next((i for i, s in enumerate(state.flat_steps)
                    if s["step"] == sn), None)
        label = f"step {sn}"
    elif pn is not None:
        idx = next((i for i, s in enumerate(state.flat_steps)
                    if s["page"] == pn), None)
        label = f"page {pn}"
    else:  # no number named — locate whatever step is on screen now
        idx = max(state.step_idx, 0)
        label = None
        # the current step is almost always already cached (its highlights are
        # on screen) → answer instantly; only wait on a push-prefetch if not
        if not _is_hot(state, ("step", idx)):
            await _await_prefetch(state)
    if idx is None:
        return f"I don't have any parts listed for {label}."
    state.step_idx = idx
    return await _locate(state, websocket, step_idx=idx)


async def _tool_find_piece(state: Session, websocket, description) -> str:
    desc = str(description or "").strip()
    if not desc:
        return "Tell me which piece you're looking for — color and size help."
    return await _locate(state, websocket,
                         adhoc={"description": desc, "quantity": 1})


async def _tool_check_piece(state: Session, websocket) -> str:
    """Is the builder holding the right piece? Looks at the full camera frame
    (the held piece can be anywhere, not just over the pile) and compares
    against the current step's parts list. The say sentence is composed here,
    not by the agent."""
    del websocket  # no grid highlights to push — the verdict is spoken only
    if not state.flat_steps:
        return NO_MANUAL_SAY
    if state.step_idx < 0:
        return ("We haven't started a step yet, so I don't know what to check "
                "against. Say next to get the first step going.")
    frames = await _fresh_frames(state)
    scene = frames and (frames.get("scene") or frames.get("pile"))
    if not scene:
        return NO_SCENE_SAY
    pieces = state.flat_steps[state.step_idx]["pieces"]
    result = await _check_piece(scene, pieces)
    say = _check_speech(result, pieces)
    if result.get("notes") and result.get("verdict") != "unsure":
        say += f" {_speakable(result['notes'])}"
    return say


def _tool_defs() -> list:
    return [
        gradbot.ToolDef(
            "next_step",
            "Advance to the NEXT build step and locate its pieces. The screen "
            "guide follows you. The builder almost NEVER says the literal word "
            "'next' — call this on ANY signal that they're moving on, however "
            "casual: 'let's move on', 'let's get to the next step', 'on to the "
            "next one', 'okay, done', 'what's next', 'keep going', 'move on', "
            "'ready for the next', 'got it, what now', 'this one's done', "
            "'let's build', 'let's go'. When in doubt between this and "
            "find_step, if they've finished the current step and want to "
            "continue, it's next_step.",
            json.dumps({"type": "object", "properties": {}}),
        ),
        gradbot.ToolDef(
            "find_step",
            "Locate the pieces for the step the builder is on RIGHT NOW — the "
            "page currently showing on screen — WITHOUT advancing. Call with no "
            "arguments for 'what about this page', 'where are these', 'find "
            "these', 'what do I need here', 'show me again', 'where'd those "
            "go'. Pass step_number or page_number ONLY when they say a specific "
            "number out loud ('step twelve' becomes step_number '12'). If they "
            "want to MOVE ON to the next step, use next_step instead.",
            json.dumps({
                "type": "object",
                "properties": {
                    "step_number": {
                        "type": "string",
                        "description": "Step number the builder said, as digits",
                    },
                    "page_number": {
                        "type": "string",
                        "description": "Page number the builder said, as digits",
                    },
                },
            }),
        ),
        gradbot.ToolDef(
            "find_one_piece",
            "Search the pile for one specific piece the builder describes. "
            "Call when they ask where a particular piece is, like 'where is "
            "the red 2 by 4 brick'. Include the color and size in the "
            "description.",
            json.dumps({
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "The piece, with color and size, "
                                       "e.g. 'red 2x4 brick'",
                    },
                },
                "required": ["description"],
            }),
        ),
        gradbot.ToolDef(
            "check_piece",
            "Look at the piece the builder is holding and say whether it's "
            "one this step needs. Call IMMEDIATELY whenever they ask about a "
            "piece in their hand — is this the right one, is this it, did I "
            "grab the right piece, which piece is this. Never ask them to "
            "hold it up first: the result handles a piece the camera can't "
            "see.",
            json.dumps({"type": "object", "properties": {}}),
        ),
        gradbot.ToolDef(
            "switch_language",
            "Switch the spoken language when the builder speaks or asks for "
            "another language (English, French, Spanish, German, Portuguese). "
            "Call this BEFORE replying, then continue in the new language.",
            json.dumps({
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "fr", "es", "de", "pt"],
                        "description": "Language code to switch to",
                    },
                },
                "required": ["language"],
            }),
        ),
    ]


# Per-language spoken-STYLE guidance for the direction sentences the tools
# hand over in English. The agent translates the "say" text; these rules keep
# it sounding like a person giving directions, not a literal gloss — mirrors
# the restaurant demo's cashier guidance. Numbers, colors, and sizes always
# stay exact; only the phrasing loosens.
LANGUAGE_STYLE_GUIDANCE = {
    "fr": """FRENCH — natural spoken directions:
- Translate the meaning the way a French person actually gives directions, never word-for-word.
- "close to you" → "près de vous"; "at the far side" → "au fond"; "in the middle" → "au milieu".
- "on your right/left" → "sur votre droite / sur votre gauche"; "right in front of you" → "juste devant vous".
- BAD: "proche à toi, sur ton droit" (literal, wrong). GOOD: "près de vous, sur votre droite".
- Keep every number, color, and piece size exact ("2 by 4" → "2 par 4").""",
    "es": """SPANISH — natural spoken directions:
- Give directions the way a Spanish speaker naturally would, not a literal gloss.
- "close to you" → "cerca de ti"; "at the far side" → "al fondo"; "in the middle" → "en el centro".
- "on your right/left" → "a tu derecha / a tu izquierda"; "right in front of you" → "justo delante de ti".
- Keep every number, color, and piece size exact ("2 by 4" → "2 por 4").""",
    "de": """GERMAN — natural spoken directions:
- Give directions the way a German speaker naturally would, not a literal gloss.
- "close to you" → "nah bei dir"; "at the far side" → "hinten"; "in the middle" → "in der Mitte".
- "on your right/left" → "rechts von dir / links von dir"; "right in front of you" → "direkt vor dir".
- Keep every number, color, and piece size exact ("2 by 4" → "2 mal 4").""",
    "pt": """PORTUGUESE — natural spoken directions:
- Give directions the way a Portuguese speaker naturally would, not a literal gloss.
- "close to you" → "perto de você"; "at the far side" → "no fundo"; "in the middle" → "no meio".
- "on your right/left" → "à sua direita / à sua esquerda"; "right in front of you" → "bem à sua frente".
- Keep every number, color, and piece size exact ("2 by 4" → "2 por 4").""",
}


def _agent_prompt(state: Session) -> str:
    if state.flat_steps:
        context = (f"A parsed set is loaded with {len(state.flat_steps)} steps, "
                   f"so exact piece lists and counts come from it; the camera "
                   f"only has to find those pieces in the pile.")
    else:
        context = ("No set is loaded yet — until the builder uploads their "
                   "instructions PDF you can't find any pieces, so guide them "
                   "to do that first.")
    if not state.flat_steps:
        setup = ("\nSETUP FIRST: greet them in one line, then ask them to "
                 "upload their set's instructions PDF on the screen. Don't "
                 "call tools until a set is loaded — there's nothing to find "
                 "yet. You'll get updated instructions the moment it's ready.")
    elif state.regions_ready:
        # the sample greeting is English-only: a non-English session would
        # dutifully speak the example verbatim and THEN translate it
        example = (" — like: \"Ready when you are — say let's build and I'll "
                   "find your pieces.\"" if state.lang == "en" else
                   ", inviting them to say let's build when they're ready.")
        setup = ("\nGreet them with ONE short line that invites them to just "
                 f"talk{example}")
    else:
        setup = ("\nSETUP FIRST: the set is loaded but the camera isn't "
                 "watching the pile yet. Greet them in one line, then ask them "
                 "to start the camera and point it at their pile of pieces. "
                 "Don't call lookup tools until the camera is ready — you'll "
                 "get updated instructions the moment it is.")
    lang_rule = ""
    if state.lang != "en":
        lang_name = gradbot.langs.LANGUAGE_NAMES.get(state.lang, state.lang)
        guidance = LANGUAGE_STYLE_GUIDANCE.get(state.lang, "")
        lang_rule = (f"\n\nThe builder speaks {lang_name} — speak ONLY "
                     f"{lang_name}. Tool \"say\" text arrives in English: "
                     f"translate it into {lang_name} faithfully, keeping "
                     f"every number, color, and piece size exact, and never "
                     f"swapping left and right or near and far.\n{guidance}")
    return f"""\
You are the voice of a hands-free LEGO building companion on a live voice call.
The builder follows the instructions ON THEIR SCREEN — you never read a
booklet. A webcam watches only their messy PILE of loose pieces. Your tools
find this step's pieces in the pile and say where they are, from the builder's
own point of view. {context}{setup}

HOW TO TALK — their hands are full of bricks and they're watching the screen:
- Bring a little energy — you genuinely love building this thing with them.
  Warm, upbeat, a bit playful. Punch a find or a finished step with a quick,
  real hype beat — "oh nice", "boom, there it is", "you got it". Snappy
  excitement, never a speech.
- One or two short, natural spoken sentences. Contractions are good. Never
  lists, never headings.
- Right before you call a tool, say a tiny acknowledgment and vary it — "On
  it.", "Let me look.", "Ooh, let's find it." — then call the tool
  immediately. Never announce a lookup without actually calling the tool.
- The excitement rides in your OWN words (the ack and the one line you add
  after) — never change the tool's "say" sentence to add flavor.

HARD RULES — never break these:
1. Never guess where a piece is, what a step needs, or whether a held piece
   is the right one. That information comes ONLY from tool results.
2. When a tool result has a "say" field, speak that text EXACTLY as written —
   never reword, reorder, or change direction words like left, right, close,
   or far. You may add one short friendly sentence after it.
3. Follow-ups about what you already told them — how many, which color, say
   it again, where was that one — answer from your last tool result, word for
   word where it matters, WITHOUT calling a tool again. Only re-run a tool
   when they want a fresh look: they moved the pile, moved to another step,
   or ask you to check again.
4. When they wonder about a piece in their hand, call check_piece right away —
   never tell them to hold it up first, and never answer from memory; the
   tool's answer covers a piece the camera can't see.
5. The builder almost never says the literal word "next". Treat ANY "I'm
   ready to continue" — "let's move on", "on to the next", "okay done",
   "what's next", "keep going" — as a cue to call next_step; the screen guide
   follows you. "What about this page / where are these" means find_step
   (the current page), NOT advancing.
6. Speak numbers as words, and say "2 by 4", never "2 x 4".
7. Off-topic chat gets one friendly sentence, then steer back to the build.\
{lang_rule}
"""


def _voice_for_lang(lang: str) -> str:
    """Session voice: env override per language, the configured default for
    English, else the first flagship voice speaking that language."""
    override = os.getenv(f"GRADIUM_VOICE_ID_{lang.upper()}")
    if override:
        return override
    if lang == "en":
        return GRADIUM_VOICE_ID
    try:
        voice = next(v for v in gradbot.flagship_voices()
                     if v.language.code() == lang)
        return voice.voice_id
    except Exception:
        log.warning("no flagship voice for %s — using the default", lang)
        return GRADIUM_VOICE_ID


def _session_config(state: Session) -> "gradbot.SessionConfig":
    lang = gradbot.langs.LANGUAGES.get(state.lang, gradbot.Lang.En)
    return gradbot.SessionConfig(
        voice_id=_voice_for_lang(state.lang),
        instructions=_agent_prompt(state),
        language=lang,
        tools=_tool_defs(),
        assistant_speaks_first=True,
        # 0.0: a builder quietly reading the screen is not dead air the agent
        # should fill (the default 5s re-prompt loops otherwise)
        silence_timeout_s=0.0,
        # the single biggest end-of-speech → first-word lever; see config
        flush_duration_s=GRADBOT_FLUSH_S,
        rewrite_rules=lang.rewrite_rules,
        llm_extra_config=LLM_EXTRA_CONFIG or None,
    )


async def _on_tool_call(state: Session, handle, input_handle, websocket) -> None:
    args = handle.args or {}
    log.info("tool %s %s (session %s)", handle.name, args, state.sid)
    try:
        if handle.name == "next_step":
            say = await _tool_next_step(state, websocket)
        elif handle.name == "find_step":
            say = await _tool_find_step(state, websocket,
                                        args.get("step_number"),
                                        args.get("page_number"))
        elif handle.name == "find_one_piece":
            say = await _tool_find_piece(state, websocket,
                                         args.get("description"))
        elif handle.name == "check_piece":
            say = await _tool_check_piece(state, websocket)
        elif handle.name == "switch_language":
            await _tool_switch_language(state, handle, input_handle, websocket,
                                        args.get("language"))
            return
        else:
            await handle.send_error(f"Unknown tool: {handle.name}")
            return
        await handle.send_json({"say": say})
    except fastapi.HTTPException as exc:
        await handle.send_json({"say": str(exc.detail)})  # already speakable
    except Exception as exc:
        log.error("tool %s failed: %s", handle.name, exc)
        await handle.send_json(
            {"say": "That lookup failed — give it another try in a moment."})


async def _tool_switch_language(state: Session, handle, input_handle,
                                websocket, lang) -> None:
    """Live language swap: rebuild the session config (new voice + translated
    instructions) via send_config without resetting the conversation, tell the
    UI to move its highlighted button, and instruct the LLM to continue in the
    new language. No "say" field — the agent picks its own words in-language."""
    if lang not in gradbot.langs.LANGUAGES:
        await handle.send_json(
            {"say": "I can speak English, French, Spanish, German, or "
                    "Portuguese — which would you like?"})
        return
    state.lang = lang
    await input_handle.send_config(_session_config(state))
    if websocket is not None:
        try:
            await websocket.send_json({"type": "lang", "lang": lang})
        except Exception:
            pass
    lang_name = gradbot.langs.LANGUAGE_NAMES.get(lang, lang)
    guidance = LANGUAGE_STYLE_GUIDANCE.get(lang, "")
    await handle.send_json({
        "switched_to": lang,
        "message": (f"Now speaking {lang_name}. Reply ONLY in {lang_name} from "
                    f"here on, keeping every number, color, and size exact. "
                    f"{guidance}").strip(),
    })


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket):
    holder: dict[str, Session] = {}

    def on_start(msg: dict) -> gradbot.SessionConfig:
        state = Session(sid=str(msg.get("sid") or os.urandom(8).hex()))
        if msg.get("orientation") in ("facing", "overhead"):
            state.orientation = msg["orientation"]
        if msg.get("lang") in gradbot.langs.LANGUAGES:
            state.lang = msg["lang"]
        state.manual_id = msg.get("manual_id") or None
        state.flat_steps = _load_flat_steps(state.manual_id)
        state.regions_ready = bool(msg.get("regions_ready"))
        state.websocket = websocket
        SESSIONS[state.sid] = state
        holder["state"] = state
        log.info("voice session %s: %d manual steps, orientation=%s, lang=%s",
                 state.sid, len(state.flat_steps), state.orientation, state.lang)
        return _session_config(state)

    def on_config(msg: dict) -> gradbot.SessionConfig:
        # The browser re-sends config when the camera setup changes mid-call
        # (regions ready) or the builder taps a language button. send_config
        # swaps voice/instructions/tools without resetting the conversation or
        # re-greeting.
        state = holder.get("state")
        if state is None:
            raise RuntimeError("Session not started yet.")
        if msg.get("orientation") in ("facing", "overhead"):
            state.orientation = msg["orientation"]
        if msg.get("lang") in gradbot.langs.LANGUAGES:
            state.lang = msg["lang"]
        if "regions_ready" in msg:
            state.regions_ready = bool(msg.get("regions_ready"))
        log.info("voice session %s: config refresh (regions_ready=%s, lang=%s)",
                 state.sid, state.regions_ready, state.lang)
        return _session_config(state)

    async def on_tool_call(handle, input_handle, ws) -> None:
        state = holder.get("state")
        if state is None:
            await handle.send_error("Session not started yet.")
            return
        await _on_tool_call(state, handle, input_handle, ws)

    try:
        await gradbot.websocket.handle_session(
            websocket,
            config=GRADBOT_CFG,
            on_start=on_start,
            on_config=on_config,
            on_tool_call=on_tool_call,
        )
    finally:
        state = holder.get("state")
        if state is not None:
            SESSIONS.pop(state.sid, None)
            state.websocket = None
            if state.prefetch_task and not state.prefetch_task.done():
                state.prefetch_task.cancel()
            for t in list(state.warm_tasks):
                if not t.done():
                    t.cancel()


# serves static/index.html at "/", the app files at /static, and the bundled
# gradbot audio JS (opus encoder, worklet, synced player) at /static/js
gradbot.routes.setup(
    app,
    config=GRADBOT_CFG,
    static_dir=pathlib.Path(__file__).parent / "static",
)
