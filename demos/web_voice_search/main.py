"""Voice web-search agent — Gradium voice + Keenable realtime web search.

A fast, focused voice assistant: the user asks a question out loud, the agent
searches the live web via Keenable (realtime mode), and answers in a sentence or
two while streaming the source cards to the browser.

Run locally:
    uv sync
    cp .env.example .env   # fill in KEENABLE_API_KEY, GRADIUM_API_KEY, LLM_*
    uv run uvicorn main:app --reload --port 8060
Then open http://localhost:8060/
"""

import json
import logging
import pathlib
from datetime import datetime

import fastapi
from dotenv import load_dotenv

# Populate os.environ from .env BEFORE importing gradbot — gradbot reads its keys
# (GRADIUM_API_KEY, LLM_API_KEY/LLM_BASE_URL/LLM_MODEL) and keenable_search reads
# KEENABLE_API_KEY directly from the environment.
load_dotenv(pathlib.Path(__file__).parent / ".env")

import gradbot  # noqa: E402

from keenable_search import (  # noqa: E402
    KeenableNotConfiguredError,
    KeenableSearchError,
    keenable_search,
)

gradbot.init_logging()
logger = logging.getLogger(__name__)

app = fastapi.FastAPI(title="Voice Web Search")
cfg = gradbot.config.from_env()

DEFAULT_VOICE_ID = "YTpq7expH9539ERJ"  # Emma

SYSTEM_PROMPT_TEMPLATE = """You are a fast, friendly voice assistant with live \
access to the web through a `web_search` tool. You are on a phone call, so keep \
everything short and conversational.

Today's date is {today}. Use it to keep searches current: when a question is \
about recent or "latest" events, put the current year (and month when relevant) \
in your query — e.g. search "latest ... {year}", never an old year.

How you work:
- For ANY question about facts, current events, prices, people, products, news, \
or anything you are not 100% certain of, you MUST call the `web_search` tool. \
Never answer such questions from memory and never guess.
- Call `web_search` immediately as your FIRST action, before saying anything. \
Never use filler — do NOT say "let me check", "one sec", "hold on", or announce \
that you are searching. Stay silent and just call the tool; the search is fast.
- Only after the tool returns results, speak your answer: 1-3 short sentences, \
mentioning the source site by name (e.g. "according to Reuters"). Never read URLs \
aloud.
- If the search returns nothing useful or fails, say you couldn't find it right \
now — don't make something up.
- Stay on task: you're here to answer questions using the web. Politely steer \
small talk back to "what would you like me to look up?"."""


def _system_prompt() -> str:
    now = datetime.now()
    return SYSTEM_PROMPT_TEMPLATE.format(
        today=now.strftime("%A, %B %-d, %Y"), year=now.year
    )


def _build_tools() -> list[gradbot.ToolDef]:
    return [
        gradbot.ToolDef(
            name="web_search",
            description=(
                "Search the live web for current or factual information. Use this "
                "for any question about recent events, facts, prices, people, "
                "products, news, or anything you are unsure about. Returns the top "
                "results with title, source site, and a snippet."
            ),
            parameters_json=json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The search query as a clear natural-language "
                                "description of what to find."
                            ),
                        }
                    },
                    "required": ["query"],
                }
            ),
        )
    ]


TOOLS = _build_tools()


def make_config(msg: dict) -> gradbot.SessionConfig:
    voice_id = msg.get("voice_id") or DEFAULT_VOICE_ID
    language = msg.get("language") or "en"
    return gradbot.SessionConfig(
        voice_id=voice_id,
        language=gradbot.LANGUAGES.get(language) if language else None,
        instructions=_system_prompt(),
        tools=TOOLS,
        **(
            {
                "assistant_speaks_first": True,
                # Gentler turn-taking: backchannels ("yeah", "mm-hmm") don't
                # cut the assistant off while it reads out search results.
                "ignore_backchannels": True,
            }
            | cfg.session_kwargs
        ),
    )


async def handle_tool_call(handle, input_handle, websocket: fastapi.WebSocket):
    """Dispatch the agent's tool calls. Only `web_search` is defined."""
    if handle.name != "web_search":
        await handle.send_error(f"Unknown tool: {handle.name}")
        return

    query = (handle.args.get("query") or "").strip()
    if not query:
        await handle.send_error("web_search requires a non-empty query.")
        return

    # Tell the browser a search has started (drives the live sources panel).
    await websocket.send_json({"type": "tool_started", "tool": "web_search", "query": query})
    logger.info("web_search: %r", query)

    try:
        results = await keenable_search(query, limit=5)
    except KeenableNotConfiguredError as exc:
        await handle.send(
            json.dumps(
                {
                    "success": False,
                    "message": (
                        "Web search isn't configured right now — tell the caller you "
                        "can't reach the web at the moment."
                    ),
                }
            )
        )
        logger.warning("web_search not configured: %s", exc)
        return
    except KeenableSearchError as exc:
        await handle.send(
            json.dumps(
                {
                    "success": False,
                    "message": (
                        "The web search failed — tell the caller you couldn't reach "
                        "the web right now and offer to try again."
                    ),
                }
            )
        )
        logger.warning("web_search failed: %s", exc)
        return

    # Stream the source cards to the browser.
    await websocket.send_json({"type": "search_results", "query": query, "results": results})

    if not results:
        await handle.send(
            json.dumps(
                {
                    "success": True,
                    "query": query,
                    "results_summary": "No results found.",
                    "message": "Tell the caller you couldn't find anything on that.",
                }
            )
        )
        return

    summary = "\n".join(
        f"- {r['title']} ({r['source']}): {r['snippet'][:300]}" for r in results
    )
    await handle.send(
        json.dumps(
            {
                "success": True,
                "query": query,
                "results_summary": summary,
                "message": (
                    "Answer the caller's question in 1-3 short spoken sentences using "
                    "these results. Mention the source site by name. Do not read URLs "
                    "aloud."
                ),
            }
        )
    )
    logger.info("web_search '%s': %d results returned", query, len(results))


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket):
    await gradbot.websocket.handle_session(
        websocket,
        config=cfg,
        on_start=make_config,
        on_tool_call=handle_tool_call,
    )


gradbot.routes.setup(
    app,
    config=cfg,
    static_dir=pathlib.Path(__file__).parent / "static",
    with_voices=True,
)
