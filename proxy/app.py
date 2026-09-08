"""Stalling-pattern dual-LLM proxy for gradbot voice agents.

Architecture
============

For every chat-completions request, we run TWO upstreams in parallel:

* **M2-her** (low-latency, ~600ms TTFB) emits a single short upbeat stall
  sentence ("Got your spicy chicken — coming right up!") to give TTS something
  to say IMMEDIATELY. Her output is discarded from conversation history.

* **MiniMax-M2.7** (the brain, ~2-3s thinking) sees the real conversation +
  tools and generates the actual reply via ``reasoning_split=True``:
    - ``delta.reasoning_details[*].text`` → discarded (silent thinking)
    - ``delta.content`` → the real answer / tool_calls

Stream merge rules
------------------
1. Forward M2-her's content immediately to the client.
2. As soon as M2.7 starts emitting ``content`` (not reasoning), wait for
   M2-her's current sentence to finish (next ``.``/``?``/``!``), then switch
   to streaming M2.7.
3. If M2-her finishes naturally before M2.7 starts → just wait silently for
   M2.7. (her did her job; bridge is short.)
4. If M2.7 only emits tool_calls (no content), drop her output entirely and
   forward only the tool_calls (gradbot will trigger a follow-up turn).
5. M2-her output is NEVER added to history — gradbot writes back assistant
   messages from what it received, but since we only emit her stall before
   m2.7 takes over, the assistant message stored in history is the merged
   stream. To prevent stalls polluting future turns, downstream history is
   the responsibility of M2.7's content alone — see ``_strip_stall_from_history``
   below for incoming messages.

Both upstreams hit MiniMax's OpenAI-compatible endpoint.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.minimax.io/v1")

STALL_MODEL = os.getenv("STALL_MODEL", "M2-her")
BRAIN_MODEL = os.getenv("BRAIN_MODEL", "MiniMax-M2.7")

STALL_TIMEOUT_S = float(os.getenv("STALL_TIMEOUT_S", "8.0"))
BRAIN_TIMEOUT_S = float(os.getenv("BRAIN_TIMEOUT_S", "30.0"))

# Sentinel marker we tag onto any assistant message that originated from a
# stall, so we can scrub it from history on the next turn. (gradbot writes
# back what we transmitted as the assistant turn.) Embedded as a zero-width
# sequence the TTS won't render and an LLM is unlikely to invent.
STALL_TAG_OPEN = "\u200b\u200c"  # zero-width space + zero-width non-joiner
STALL_TAG_CLOSE = "\u200c\u200b"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("proxy")


# ---------------------------------------------------------------------------
# HTTP client lifecycle
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
    )
    logger.info(
        "proxy ready: upstream=%s stall=%s brain=%s",
        UPSTREAM_BASE_URL, STALL_MODEL, BRAIN_MODEL,
    )
    try:
        yield
    finally:
        await _http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Stall prompt — M2-her sees the real conversation but only stalls
# ---------------------------------------------------------------------------

STALL_SYSTEM_PROMPT = """You are an ECHO-CONFIRMER for a fast-food cashier voice agent.

A SMARTER model is composing the real reply (with menu data, prices, totals,
and tool calls) but takes 2-3 seconds. Your sole job: emit ONE short upbeat
sentence that buys time so the customer hears something IMMEDIATELY.

═══════════════════════════════════════════════════════════════
RESTAURANT CONTEXT (read-only — DO NOT recite or list)
═══════════════════════════════════════════════════════════════
This is a fast-food restaurant. The smarter model can: show the menu,
add/remove/modify items, view order, place order, switch language. The menu
has 4 categories: sandwiches, sides, drinks, desserts. The smarter model
owns ALL real menu data, prices, ingredients, item names, and totals.

This context exists ONLY to help you sound grounded — never to answer with.

═══════════════════════════════════════════════════════════════
OUTPUT TEMPLATE (mandatory — one sentence, then STOP)
═══════════════════════════════════════════════════════════════
   "<ack>, <short echo of user's last sentence>, <stall>!"

ack:    Got it | Sure thing | Alright | Awesome | Perfect | Sounds good
stall:  working on that | one quick sec | on it now | coming right up |
        putting that through | pulling that up

ECHO RULES (the middle part):
  - Mirror what the user JUST said in 3-7 words. Stay close to their wording.
  - You MAY repeat an item NAME the user spoke verbatim (e.g.
    "classic chicken sandwich" if they said it).
  - You MAY name the category if user explicitly asked for it.
  - You MAY NOT invent any item, side, drink, dessert, ingredient, sauce,
    size, or variant they did not say themselves.

═══════════════════════════════════════════════════════════════
EXAMPLES
═══════════════════════════════════════════════════════════════
User: "Show me the menu."
You:  "Got it, pulling up the menu, one quick sec!"

User: "I want a classic chicken sandwich."
You:  "Sure thing, one classic chicken sandwich, on it now!"

User: "Add a coffee."
You:  "Alright, one coffee, working on that!"

User: "What's my total?"
You:  "Got it, checking your total, one quick sec!"

User: "I'm done, that's it."
You:  "Sounds good, wrapping that up, putting that through!"

User: "My name is David."
You:  "Perfect, name David, on it now!"

User: "I'd like the spicy chicken with extra pickles."
You:  "Got it, spicy chicken with extra pickles, working on that!"

═══════════════════════════════════════════════════════════════
HARD CONSTRAINTS
═══════════════════════════════════════════════════════════════
- ONE sentence. Then STOP.
- NEVER state any number, price, total, "$", or digit.
- NEVER invent an item the user did not say.
- NEVER claim an action completed ("added", "placed") — use IN-PROGRESS form.
- NEVER ask a real follow-up question with content. Generic "anything else?"
  only allowed if user just added an item.
- NEVER say "Hi", "Hello".
- NEVER write *stage directions*, asterisks, parentheses, markdown, or
  roleplay. Plain spoken text only.
- NEVER pretend to be a different persona (store owner, interviewer,
  bartender). You are the cashier's stall talker, full stop.

If user message is empty / unclear / off-topic, output: "Sure thing, one quick sec!"
"""


# ---------------------------------------------------------------------------
# History scrubbing — strip stall sentences from prior assistant turns
# ---------------------------------------------------------------------------

def _strip_stall_from_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove stall fragments from past assistant messages so the brain only
    sees its own real replies as history.

    Output convention from this proxy:
        assistant.content = "[stall sentence][CLOSE marker][brain reply]"

    So the scrub rule is: if CLOSE marker is present, delete everything up to
    and including it, leaving only the brain reply. If only OPEN appears (rare
    edge case), drop everything from OPEN onward (treat as truncated stall).
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            cleaned.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str) and (STALL_TAG_CLOSE in content or STALL_TAG_OPEN in content):
            new_content = content
            if STALL_TAG_CLOSE in new_content:
                # Drop stall + marker, keep brain reply.
                new_content = new_content[new_content.find(STALL_TAG_CLOSE) + len(STALL_TAG_CLOSE):]
            if STALL_TAG_OPEN in new_content:
                new_content = new_content[: new_content.find(STALL_TAG_OPEN)]
            new_content = new_content.strip()
            if new_content or msg.get("tool_calls"):
                new_msg = dict(msg)
                new_msg["content"] = new_content
                cleaned.append(new_msg)
            # else drop entirely (was pure stall, nothing useful)
        else:
            cleaned.append(msg)
    return cleaned


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        s = (c or "").strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

def _build_stall_body(req_body: dict[str, Any]) -> dict[str, Any]:
    """Body for M2-her stall: STATELESS, NO history, NO tools, NO menu data.

    The stall talker physically cannot see business state, so it physically
    cannot hallucinate prices, items, or claim actions. Only thing it sees:
    a one-line system prompt + the customer's most recent utterance.
    """
    user_text = _last_user_text(req_body.get("messages") or [])
    if not user_text:
        user_text = "(silence)"
    return {
        "model": STALL_MODEL,
        "stream": True,
        "messages": [
            {"role": "system", "content": STALL_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 50,
        "temperature": 0.5,
    }


def _build_brain_body(req_body: dict[str, Any]) -> dict[str, Any]:
    """Body for M2.7: full conversation, tools, reasoning_split=True.

    History is scrubbed of any stall fragments so the brain only sees its
    own real prior replies.

    A small system-prompt amendment tells M2.7 that a parallel "stall talker"
    has ALREADY said the customer-facing acknowledgement, so M2.7's content
    must skip the filler and dive straight into the substantive reply
    (numbers, items, follow-up question).
    """
    body = copy.deepcopy(req_body)
    body["model"] = BRAIN_MODEL
    body["stream"] = True
    body["messages"] = _strip_stall_from_history(body.get("messages") or [])
    body["reasoning_split"] = True

    # Append brain-side awareness of the stall pipeline.
    msgs = body["messages"]
    if msgs and msgs[0].get("role") == "system":
        amendment = (
            "\n\n=== STALLING-PIPELINE AWARENESS ===\n"
            "A separate fast 'stall' model is already speaking a brief upbeat "
            "acknowledgement to the customer in parallel with you (e.g. \"Sure thing, "
            "let me grab that!\", \"Got it — adding that for you!\", \"Coming right up!\"). "
            "By the time YOUR content reaches the speaker, the customer has already "
            "heard that filler. Therefore:\n"
            "- DO NOT open with \"Sure\", \"Got it\", \"Alright\", \"Coming right up\", "
            "\"Let me\", \"One sec\", \"Here you go\", or any other acknowledgement / "
            "filler — the stall said it. Repeating it sounds like the agent stuttered.\n"
            "- DIVE STRAIGHT into the substantive content: name the item just added, "
            "state the price/total, ask the next question, etc.\n"
            "- Examples of correct openings:\n"
            "    after stall + add_to_order: \"That's a Classic Chicken Sandwich. Anything else?\"\n"
            "    after stall + place_order: \"Subtotal $10.19, tax $0.71, total $10.90. Ready in 5!\"\n"
            "    after stall + show_menu: \"We've got sandwiches, sides, drinks, desserts. What sounds good?\"\n"
            "- Keep replies SHORT (1-2 sentences). The customer is on a voice call.\n"
            "- Never write stage directions, asterisks, or markdown.\n"
            "=== END AWARENESS ===\n"
        )
        first = dict(msgs[0])
        first["content"] = (first.get("content") or "") + amendment
        msgs[0] = first
        body["messages"] = msgs

    return body


def _strip_tool_plumbing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool calls/results — M2-her chokes on them."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content = (msg.get("content") or "").strip()
            if content:
                out.append({"role": "assistant", "content": content})
            continue
        clean = {k: v for k, v in msg.items() if k != "tool_calls"}
        if clean.get("content") is None:
            clean["content"] = ""
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
# SSE chunk helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
) -> bytes:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta or {}, "finish_reason": finish_reason}
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Stall stream — async generator yielding text chunks
# ---------------------------------------------------------------------------

async def _stall_stream(
    body: dict[str, Any],
    auth_token: str,
    request_id: str,
    text_out: asyncio.Queue,
    done_event: asyncio.Event,
) -> None:
    """Run the stall request and push CLEAN text deltas into ``text_out``.

    Defensive scrub: M2-her sometimes emits ``*beep*`` style stage directions
    despite the prompt forbidding it. We strip ``*...*`` spans (and orphan
    asterisks) from each chunk before publishing. Cross-chunk spans are
    handled by holding back any text after an unmatched ``*``.

    Sends a sentinel ``None`` and sets ``done_event`` when finished. Errors
    are swallowed silently — stall is best-effort.
    """
    assert _http_client is not None
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    started = time.monotonic()
    pending = ""  # carryover text past an unmatched * (held until close or stream end)
    chunks_received = 0
    last_chunk_at = started
    try:
        async with _http_client.stream(
            "POST", url, headers=headers, json=body,
            timeout=httpx.Timeout(connect=5.0, read=STALL_TIMEOUT_S, write=10.0, pool=10.0),
        ) as resp:
            if resp.status_code >= 400:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                logger.warning("[%s] stall %d: %s", request_id, resp.status_code, err[:200])
                return
            buf = b""
            ttft_logged = False
            async for raw in resp.aiter_bytes():
                if not ttft_logged:
                    logger.info(
                        "[%s] stall TTFB %.0fms",
                        request_id, (time.monotonic() - started) * 1000,
                    )
                    ttft_logged = True
                buf += raw
                while True:
                    sep = buf.find(b"\n\n")
                    if sep == -1:
                        break
                    event = buf[:sep].decode("utf-8", errors="replace")
                    buf = buf[sep + 2:]
                    for line in event.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for ch in obj.get("choices", []) or []:
                            delta = ch.get("delta") or {}
                            content = delta.get("content")
                            if not content:
                                continue
                            now = time.monotonic()
                            chunks_received += 1
                            gap_ms = (now - last_chunk_at) * 1000
                            last_chunk_at = now
                            if chunks_received <= 8:
                                logger.info(
                                    "[%s] stall chunk #%d gap=%.0fms len=%d %r",
                                    request_id, chunks_received, gap_ms,
                                    len(content), content[:40],
                                )
                            pending += content
                            cleaned, pending = _scrub_stall_text(pending)
                            if cleaned:
                                await text_out.put(cleaned)
            tail = pending.replace("*", "").strip()
            if tail:
                await text_out.put(tail)
    except (httpx.HTTPError, asyncio.CancelledError) as exc:
        logger.warning("[%s] stall stream error: %s", request_id, exc)
    finally:
        await text_out.put(None)
        done_event.set()


def _scrub_stall_text(buf: str) -> tuple[str, str]:
    """Strip *...* spans, lowercase parentheticals, AND any price/number
    tokens from ``buf``. Returns (clean_emit, carry_over).

    M2-her sometimes hallucinates prices despite the prompt forbidding it.
    Defensive: blow away anything that looks like ``$N.NN`` or a digit run
    longer than 1 (year-ish numbers OK in product names; we're aggressive).
    """
    # Strip closed * spans first.
    while True:
        i = buf.find("*")
        if i == -1:
            break
        j = buf.find("*", i + 1)
        if j == -1:
            return buf[:i], buf[i:]
        buf = buf[:i] + buf[j + 1:]
    # Strip $-prefixed amounts: $5.99 / $5 / $5,000.
    import re as _re
    buf = _re.sub(r"\$\s?\d[\d,]*(?:\.\d+)?", "", buf)
    # Strip standalone multi-digit runs (e.g. "5.99", "1099") — leave single
    # digits like "1" alone (could be position indicator).
    buf = _re.sub(r"\b\d+\.\d+\b", "", buf)
    # Lowercase parenthetical stage directions.
    out = []
    pos = 0
    while pos < len(buf):
        lp = buf.find("(", pos)
        if lp == -1:
            out.append(buf[pos:])
            break
        rp = buf.find(")", lp + 1)
        if rp == -1:
            out.append(buf[pos:lp])
            return "".join(out), buf[lp:]
        inside = buf[lp + 1: rp]
        if 0 < len(inside) <= 60 and inside[:1].isalpha() and inside[:1].islower():
            out.append(buf[pos:lp])
        else:
            out.append(buf[pos: rp + 1])
        pos = rp + 1
    return "".join(out), ""


# ---------------------------------------------------------------------------
# Brain stream — async generator yielding content + tool_calls + reasoning
# ---------------------------------------------------------------------------

class BrainResult:
    __slots__ = ("content_q", "tool_calls", "done_event", "first_content_at", "content_text")

    def __init__(self) -> None:
        self.content_q: asyncio.Queue = asyncio.Queue()
        self.tool_calls: list[dict[str, Any]] = []
        self.done_event = asyncio.Event()
        self.first_content_at: float | None = None
        self.content_text: str = ""


async def _brain_stream(
    body: dict[str, Any],
    auth_token: str,
    request_id: str,
    out: BrainResult,
) -> None:
    """Stream the brain. content deltas → out.content_q. tool_calls accumulate."""
    assert _http_client is not None
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    started = time.monotonic()
    # tool_calls are streamed in OpenAI as deltas keyed by index.
    tc_partial: dict[int, dict[str, Any]] = {}
    try:
        async with _http_client.stream(
            "POST", url, headers=headers, json=body,
            timeout=httpx.Timeout(connect=5.0, read=BRAIN_TIMEOUT_S, write=10.0, pool=10.0),
        ) as resp:
            if resp.status_code >= 400:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                logger.error("[%s] brain %d: %s", request_id, resp.status_code, err[:300])
                return
            buf = b""
            ttft_logged = False
            async for raw in resp.aiter_bytes():
                if not ttft_logged:
                    logger.info(
                        "[%s] brain TTFB %.0fms",
                        request_id, (time.monotonic() - started) * 1000,
                    )
                    ttft_logged = True
                buf += raw
                while True:
                    sep = buf.find(b"\n\n")
                    if sep == -1:
                        break
                    event = buf[:sep].decode("utf-8", errors="replace")
                    buf = buf[sep + 2:]
                    for line in event.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for ch in obj.get("choices", []) or []:
                            delta = ch.get("delta") or {}
                            # 1. content
                            txt = delta.get("content")
                            if txt:
                                if out.first_content_at is None:
                                    out.first_content_at = time.monotonic()
                                    logger.info(
                                        "[%s] brain first content @ %.2fs",
                                        request_id, out.first_content_at - started,
                                    )
                                out.content_text += txt
                                await out.content_q.put(txt)
                            # 2. tool_calls (accumulate)
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = tc_partial.setdefault(idx, {
                                    "id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                })
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if tc.get("type"):
                                    slot["type"] = tc["type"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += fn["arguments"]
                            # 3. reasoning_details — discard silently
    except (httpx.HTTPError, asyncio.CancelledError) as exc:
        logger.warning("[%s] brain stream error: %s", request_id, exc)
    finally:
        out.tool_calls = [tc_partial[i] for i in sorted(tc_partial)]
        await out.content_q.put(None)
        out.done_event.set()


# ---------------------------------------------------------------------------
# Stream merger — the core stalling logic
# ---------------------------------------------------------------------------

# Sentence-final punctuation we'll wait for before cutting in.
SENT_END = (".", "?", "!", "—")


async def _merge_streams(
    completion_id: str,
    created: int,
    advertised_model: str,
    request_id: str,
    stall_q: asyncio.Queue,
    stall_done: asyncio.Event,
    brain: BrainResult,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes following the stalling protocol.

    Output is buffered to WORD BOUNDARIES (whitespace) before being emitted as
    SSE events. Each downstream SSE event triggers a separate TTS round-trip
    in gradbot, so emitting tiny chunks like 'Ack' followed by ', show menu'
    creates audible gaps between TTS segments. Buffering until we hit a space
    or end-of-stream gives the TTS pipeline complete words and smooth audio.
    """
    started = time.monotonic()
    stall_buf = ""              # everything stall has emitted so far
    stall_emitted = ""          # everything we've forwarded to client from stall
    stall_finished = False
    cut_in_sent = False

    # Word-boundary emit buffer for the OUTGOING SSE stream.
    out_buf = ""

    def _make_text_chunk(text: str) -> bytes:
        logger.info("[%s] YIELD %d chars: %r", request_id, len(text), text[:100])
        return _make_chunk(
            completion_id=completion_id, created=created,
            model=advertised_model, delta={"content": text},
        )

    def _flush_at_word_boundary() -> bytes | None:
        """For STALL phase: hold ALL text in buffer until end-of-stream, then
        emit as a single SSE chunk. Stall is short (1 sentence) and emitting
        partial words causes gradbot to start multiple TTS streams which
        introduces audible gaps. Phase 2 (brain) emits at sentence boundaries."""
        return None  # always hold during stall, flush in _flush_all at end of stall

    def _flush_at_sentence_boundary() -> bytes | None:
        """For BRAIN phase: emit chunks at sentence boundaries (. ? !) so TTS
        can start producing audio early but doesn't get cut into syllables."""
        nonlocal out_buf
        if not out_buf:
            return None
        # Find last sentence end punctuation followed by space or end.
        last_end = -1
        for ch in SENT_END:
            idx = out_buf.rfind(ch)
            if idx > last_end:
                last_end = idx
        if last_end == -1:
            return None
        # Emit through the punctuation + any trailing space.
        cut = last_end + 1
        while cut < len(out_buf) and out_buf[cut] in (" ", "\n", "\t"):
            cut += 1
        emit = out_buf[:cut]
        out_buf = out_buf[cut:]
        return _make_text_chunk(emit) if emit else None

    def _flush_all() -> bytes | None:
        """Emit whatever's in out_buf regardless of boundary (end of stream)."""
        nonlocal out_buf
        if not out_buf:
            return None
        emit = out_buf
        out_buf = ""
        return _make_text_chunk(emit)

    async def _next_stall() -> str | None:
        try:
            return await asyncio.wait_for(stall_q.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return ""

    # Phase 1: stream stall to completion. Brain runs in parallel but is
    # NOT allowed to interrupt the stall — its content is held until the
    # stall stream naturally finishes (sentinel None on stall_q). This
    # eliminates audible mid-sentence cuts when brain races stall.
    while not stall_finished:
        chunk = await _next_stall()
        if chunk is None:
            stall_finished = True
            break
        if chunk:
            stall_buf += chunk
            stall_emitted = stall_buf
            out_buf += chunk
            sse = _flush_at_word_boundary()
            if sse:
                yield sse

    # Drain stall queue silently.
    asyncio.create_task(_drain_queue(stall_q))

    # Flush any remaining stall text past last space (e.g. final sentence
    # without trailing space).
    sse = _flush_all()
    if sse:
        yield sse

    # End-of-stall marker so we can scrub from history later.
    if stall_emitted.strip():
        yield _make_text_chunk(STALL_TAG_CLOSE)

    # Phase 2: stream brain content (sentence-buffered).
    while True:
        chunk = await brain.content_q.get()
        if chunk is None:
            break
        cut_in_sent = True
        out_buf += chunk
        sse = _flush_at_sentence_boundary()
        if sse:
            yield sse
    sse = _flush_all()
    if sse:
        yield sse

    # Brain done. Tool calls?
    await brain.done_event.wait()
    if brain.tool_calls:
        for idx, tc in enumerate(brain.tool_calls):
            yield _make_chunk(
                completion_id=completion_id, created=created,
                model=advertised_model,
                delta={"tool_calls": [{
                    "index": idx,
                    "id": tc["id"],
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }]},
            )
        finish = "tool_calls"
    else:
        finish = "stop"

    yield _make_chunk(
        completion_id=completion_id, created=created,
        model=advertised_model, delta={}, finish_reason=finish,
    )
    yield b"data: [DONE]\n\n"

    logger.info(
        "[%s] merge done in %.2fs (stall=%r, brain=%r, tools=%d)",
        request_id, time.monotonic() - started,
        stall_emitted[:80], brain.content_text[:120], len(brain.tool_calls),
    )


async def _drain_queue(q: asyncio.Queue) -> None:
    while True:
        item = await q.get()
        if item is None:
            return


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_body = await request.json()
    request_id = uuid.uuid4().hex[:8]
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    auth_token = auth_header.removeprefix("Bearer ").strip()
    if not auth_token:
        auth_token = os.getenv("MINIMAX_API_KEY", "")
    if not auth_token:
        return JSONResponse(
            {"error": {"message": "no API key (set MINIMAX_API_KEY or send Authorization header)"}},
            status_code=401,
        )

    advertised_model = req_body.get("model") or "routed"
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    last_user = _last_user_text(req_body.get("messages") or [])
    has_tools = bool(req_body.get("tools"))
    msgs_for_role_check = req_body.get("messages") or []
    # A request is a tool-result follow-up iff history contains AT LEAST ONE
    # tool message AND the last assistant message had tool_calls. After the
    # first user→tool_call→tool_result cycle, the LLM is being re-asked to
    # speak using the tool result — there's no fresh user input to stall for.
    has_tool_msg = any(m.get("role") == "tool" for m in msgs_for_role_check)
    last_assistant_had_tools = False
    for m in reversed(msgs_for_role_check):
        if m.get("role") == "assistant":
            last_assistant_had_tools = bool(m.get("tool_calls"))
            break
    is_followup = has_tool_msg and last_assistant_had_tools
    trailing_role = "?"
    for m in reversed(msgs_for_role_check):
        if m.get("role") == "system":
            continue
        trailing_role = m.get("role") or "?"
        break

    # Debug: log all roles to understand gradbot's message pattern
    roles = [m.get("role") for m in msgs_for_role_check]
    logger.info("[%s] roles=%s has_tool=%s last_asst_tools=%s",
                request_id, roles, has_tool_msg, last_assistant_had_tools)
    logger.info(
        "[%s] dispatch: stall=%s brain=%s tools=%d msgs=%d last_user=%r tail=%s followup=%s",
        request_id, STALL_MODEL, BRAIN_MODEL, len(req_body.get("tools") or []),
        len(req_body.get("messages") or []), last_user[:80], trailing_role, is_followup,
    )

    # Build bodies + start streams.
    brain_body = _build_brain_body(req_body)
    brain_result = BrainResult()
    brain_task = asyncio.create_task(
        _brain_stream(brain_body, auth_token, request_id, brain_result),
        name=f"brain-{request_id}",
    )

    # Skip the stall when:
    #  - it's a tool-result follow-up (no fresh user input to acknowledge), OR
    #  - last_user is empty, OR
    #  - last_user is just gradbot's session-start sentinel "[start]"
    #    (auto-pushed at WS open, no real user has spoken; M2-her with no
    #    context invents fictional cashier roleplay), OR
    #  - last_user *equals* "[start]" alone (raw startup), in which case
    #    let brain greet directly.
    stall_q: asyncio.Queue = asyncio.Queue()
    stall_done = asyncio.Event()
    skip_stall = (
        is_followup
        or not last_user
        or last_user.strip().lower() in ("[start]", "[start]\n", "start")
    )
    if skip_stall:
        await stall_q.put(None)
        stall_done.set()
        stall_task = None
    else:
        stall_body = _build_stall_body(req_body)
        stall_task = asyncio.create_task(
            _stall_stream(stall_body, auth_token, request_id, stall_q, stall_done),
            name=f"stall-{request_id}",
        )

    async def stream_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in _merge_streams(
                completion_id=completion_id,
                created=created,
                advertised_model=advertised_model,
                request_id=request_id,
                stall_q=stall_q,
                stall_done=stall_done,
                brain=brain_result,
            ):
                if await request.is_disconnected():
                    logger.info("[%s] client disconnected", request_id)
                    break
                yield chunk
        finally:
            for t in (stall_task, brain_task):
                if t and not t.done():
                    t.cancel()

    return StreamingResponse(stream_iter(), media_type="text/event-stream")
