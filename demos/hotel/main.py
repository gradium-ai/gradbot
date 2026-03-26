"""
Hotel Reservation Demo - Voice AI hotel booking agent

A voice agent that helps callers search for and book hotels anywhere in the world.
Uses Linkup web search to find real hotels and room information.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket
from linkup import LinkupClient

import gradbot
from gradbot.fastapi import websocket_chat_handler, setup_demo_routes

gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

from gradbot.demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

logger = logging.getLogger(__name__)

linkup_client = LinkupClient(
    api_key=_YAML_CFG.get("linkup", {}).get("api_key") or os.environ.get("LINKUP_API_KEY", ""),
)


async def do_search(query: str, include_images: bool = True) -> dict:
    """Run Linkup search in a thread pool, optionally fetching images."""
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: linkup_client.search(
                query=query,
                depth="standard",
                output_type="searchResults",
                include_images=include_images,
            ),
        )

        sources = []
        images = []
        for item in (result.results or []):
            if item.type == "image":
                images.append({"title": item.name, "url": item.url})
            else:
                sources.append({
                    "title": item.name,
                    "href": item.url,
                    "body": item.content[:300] if item.content else "",
                })

        # Build a text summary from text results for the LLM prompt
        answer_parts = []
        for s in sources[:5]:
            answer_parts.append(f"- {s['title']}: {s['body']}")
        answer = "\n".join(answer_parts)

        logger.info("Linkup search '%s': %d sources, %d images", query, len(sources), len(images))
        return {
            "answer": answer,
            "sources": sources,
            "images": images,
        }
    except Exception as e:
        logger.error("Linkup search '%s' failed: %s", query, e)
        return {"answer": "", "sources": [], "images": []}


@dataclass
class BookingState:
    """Tracks the current booking session."""
    current_destination: str | None = None


AGENT_VOICES = {
    "Sophie": "Eva",
    "Sydney": "Sydney",
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_BASE_PROMPT_TEMPLATE = (_PROMPTS_DIR / "base.txt").read_text()
_PHASE1_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase1.txt").read_text()
_PHASE2_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase2.txt").read_text()
_PHASE3_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase3.txt").read_text()


def _base_prompt(agent_name: str) -> str:
    """Personality + style rules shared across all phases."""
    return _BASE_PROMPT_TEMPLATE.format(agent_name=agent_name)


def get_phase1_prompt(agent_name: str, filters: dict | None = None) -> str:
    """Phase 1: Destination selection, with optional pre-filled filters."""
    prompt = _base_prompt(agent_name) + _PHASE1_PROMPT_TEMPLATE

    if filters:
        parts = []
        if filters.get("destination"):
            parts.append(f"- Destination: {filters['destination']}")
        if filters.get("check_in"):
            parts.append(f"- Check-in: {filters['check_in']}")
        if filters.get("check_out"):
            parts.append(f"- Check-out: {filters['check_out']}")
        if filters.get("travelers"):
            parts.append(f"- Travelers: {filters['travelers']}")
        if filters.get("budget") and filters["budget"] < 5000:
            parts.append(f"- Budget: up to ${filters['budget']} per night")

        if parts:
            context = "\n".join(parts)
            prompt += f"\n\nThe caller has already provided these details via the booking form:\n{context}\n"
            prompt += "Acknowledge what they've filled in. Do NOT ask again for information they already provided.\n"
            if filters.get("destination"):
                prompt += f"Since they already chose {filters['destination']}, you may call search_hotels right after your greeting (this counts as the caller choosing a destination).\n"

    return prompt


def get_phase2_prompt(agent_name: str, destination: str, search_answer: str) -> str:
    """Phase 2: Hotel selection from search results."""
    return _base_prompt(agent_name) + _PHASE2_PROMPT_TEMPLATE.format(
        destination=destination,
        search_results=search_answer,
    )


def get_phase3_prompt(agent_name: str, hotel_name: str, search_answer: str) -> str:
    """Phase 3: Room selection & booking from search results."""
    return _base_prompt(agent_name) + _PHASE3_PROMPT_TEMPLATE.format(
        hotel_name=hotel_name,
        search_results=search_answer,
    )


def build_tools() -> list[gradbot.ToolDef]:
    return [
        gradbot.ToolDef(
            name="search_hotels",
            description="Search the web for available hotels in a destination. Takes some time to search. Keep chatting with the caller about the destination while waiting for results! IMPORTANT: Always include the caller's preferences (budget, style, etc.) in the preferences field so the search is targeted.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "The city or destination to search hotels in (e.g. 'Paris', 'Bali', 'Tokyo')"
                    },
                    "preferences": {
                        "type": "string",
                        "description": "Caller's requirements: budget range, style, amenities, etc. (e.g. 'under 100 euros per night, boutique style' or 'luxury 5-star with spa')"
                    }
                },
                "required": ["destination"]
            }),
        ),
        gradbot.ToolDef(
            name="get_hotel_details",
            description="Search the web for detailed room information and prices for a specific hotel. You MUST call this before you can talk about specific rooms or prices. Takes some time - keep chatting with the caller!",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "hotel_name": {
                        "type": "string",
                        "description": "The full name of the hotel to look up"
                    },
                    "destination": {
                        "type": "string",
                        "description": "The city or destination the hotel is in"
                    }
                },
                "required": ["hotel_name"]
            }),
        ),
        gradbot.ToolDef(
            name="book_room",
            description="Book a room at a hotel. Use this when the caller has decided on a hotel, room type, dates, and number of guests.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "hotel_name": {
                        "type": "string",
                        "description": "The hotel name"
                    },
                    "room_type": {
                        "type": "string",
                        "description": "The room type (e.g. 'Deluxe Room', 'Suite')"
                    },
                    "check_in": {
                        "type": "string",
                        "description": "Check-in date (e.g. 'March 15, 2025')"
                    },
                    "check_out": {
                        "type": "string",
                        "description": "Check-out date (e.g. 'March 20, 2025')"
                    },
                    "guests": {
                        "type": "integer",
                        "description": "Number of guests"
                    },
                    "guest_name": {
                        "type": "string",
                        "description": "Name for the reservation"
                    }
                },
                "required": ["hotel_name", "room_type", "check_in", "check_out", "guests", "guest_name"]
            }),
        ),
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Hotel Reservation Demo...")
    yield
    logger.info("Shutting down...")


app = FastAPI(title="Hotel Reservation Demo", lifespan=lifespan)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    state = BookingState()
    tools = build_tools()

    # These will be set from the start message
    agent_name = "Sophie"
    padding_bonus = 0.0
    voice = None

    def make_config(instructions: str) -> gradbot.SessionConfig:
        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=instructions,
            language=gradbot.Lang.En,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="en",
            ),
        )

    async def handle_search_hotels(destination: str, preferences: str | None, tool_handle, input_handle, websocket: WebSocket):
        """Search hotels via Linkup web search."""
        await websocket.send_json({
            "type": "tool_started",
            "tool": "search_hotels",
            "message": f"Searching hotels in {destination}...",
        })

        if preferences:
            query = f"hotels in {destination} {preferences} with star ratings, prices per night, and brief descriptions"
        else:
            query = f"best hotels in {destination} with star ratings, price ranges, amenities, and brief descriptions"
        logger.info("Searching hotels in %s via Linkup (prefs: %s)", destination, preferences)
        result = await do_search(query)

        state.current_destination = destination

        # Send sources + images to frontend
        await websocket.send_json({
            "type": "search_results",
            "query": f"Hotels in {destination}",
            "results": result["sources"],
            "images": result.get("images", []),
        })

        # Swap to phase 2 prompt with search results
        phase2 = get_phase2_prompt(agent_name, destination, result["answer"])
        await input_handle.send_config(make_config(phase2))
        logger.info("Switched to phase 2 prompt for %s", destination)

        await tool_handle.send(json.dumps({
            "success": True,
            "destination": destination,
            "answer": result["answer"],
            "sources": result["sources"],
            "message": f"Hotel search results for {destination} are ready. Present the top hotel options to the caller based on the search results. Highlight star ratings, price ranges, and key features. Ask which one interests them!"
                f"\n\nREMINDER: You do NOT have specific room details yet. The INSTANT the caller picks a hotel, call get_hotel_details. Do NOT make up specific room types or exact prices.",
        }))

        logger.info("Search complete for %s: %d sources", destination, len(result["sources"]))

    async def handle_hotel_details(hotel_name: str, destination: str | None, tool_handle, input_handle, websocket: WebSocket):
        """Get hotel details via Linkup web search."""
        await websocket.send_json({
            "type": "tool_started",
            "tool": "get_hotel_details",
            "message": f"Loading details for {hotel_name}...",
        })

        dest = destination or state.current_destination or ""
        query = f"{hotel_name} {dest} hotel room types, prices per night, room descriptions, and amenities"
        logger.info("Loading details for %s via Linkup", hotel_name)
        result = await do_search(query)

        # Send sources to frontend
        await websocket.send_json({
            "type": "search_results",
            "query": f"Rooms at {hotel_name}",
            "results": result["sources"],
            "images": result.get("images", []),
        })

        # Swap to phase 3 prompt with search results
        phase3 = get_phase3_prompt(agent_name, hotel_name, result["answer"])
        await input_handle.send_config(make_config(phase3))
        logger.info("Switched to phase 3 prompt for %s", hotel_name)

        await tool_handle.send(json.dumps({
            "success": True,
            "hotel_name": hotel_name,
            "answer": result["answer"],
            "sources": result["sources"],
            "message": f"Room details for {hotel_name} are ready. Present the room options with prices from the search results. Help the caller choose!",
        }))

        logger.info("Details loaded for %s", hotel_name)

    async def handle_book_room(args: dict, tool_handle, websocket: WebSocket):
        """Process a simulated booking."""
        confirmation = f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{random.randint(1000, 9999)}"

        booking_info = {
            "confirmation_number": confirmation,
            "hotel": args["hotel_name"],
            "room": args["room_type"],
            "check_in": args["check_in"],
            "check_out": args["check_out"],
            "guests": args["guests"],
            "guest_name": args["guest_name"],
        }

        await websocket.send_json({
            "type": "booking_confirmed",
            "booking": booking_info,
        })

        await tool_handle.send(json.dumps({
            "success": True,
            "booking": booking_info,
            "message": f"Booking confirmed! Confirmation number: {confirmation}. Congratulate the caller and summarize their reservation details.",
        }))

        logger.info("Booking confirmed: %s", confirmation)

    async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        logger.info("Tool call: %s - %s", tool_name, args)

        if tool_name == "search_hotels":
            destination = args.get("destination", "").strip()
            preferences = args.get("preferences")
            await handle_search_hotels(destination, preferences, tool_handle, input_handle, websocket)

        elif tool_name == "get_hotel_details":
            hotel_name = args.get("hotel_name", "")
            destination = args.get("destination")
            await handle_hotel_details(hotel_name, destination, tool_handle, input_handle, websocket)

        elif tool_name == "book_room":
            await handle_book_room(args, tool_handle, websocket)

        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")

    def on_start(msg: dict) -> gradbot.SessionConfig:
        nonlocal agent_name, padding_bonus, voice
        agent_name = msg.get("agent", "Sophie")
        padding_bonus = float(msg.get("padding_bonus", 0.0))
        voice_key = AGENT_VOICES.get(agent_name, "Eva")
        voice = gradbot.flagship_voice(voice_key)

        filters = {
            "destination": msg.get("destination", ""),
            "check_in": msg.get("check_in", ""),
            "check_out": msg.get("check_out", ""),
            "travelers": msg.get("travelers", 2),
            "budget": msg.get("budget", 5000),
        }
        logger.info("Starting hotel reservation chat with %s (voice: %s, filters: %s)",
                     agent_name, voice_key, filters)

        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_phase1_prompt(agent_name, filters),
            language=gradbot.Lang.En,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="en",
                assistant_speaks_first=True,
            ),
        )

    await websocket_chat_handler(
        websocket,
        on_start=on_start,
        on_tool_call=handle_tool_call,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


setup_demo_routes(app, static_dir=Path(__file__).parent / "static", use_pcm=USE_PCM)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
