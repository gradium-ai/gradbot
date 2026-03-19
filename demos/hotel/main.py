"""
Hotel Reservation Demo - Voice AI hotel booking agent

A voice agent that helps callers search for and book hotels in Paris, Bali, and Dubai.
Searches are deferred (10-20s random delay) to demonstrate chit-chat while waiting.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket

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


def compact_phone(phone: str) -> str:
    """Strip spaces and dashes from phone number so TTS reads it as a block."""
    return phone.replace(" ", "").replace("-", "")


# Load hotel data
HOTELS_PATH = Path(__file__).parent / "hotels.json"
with open(HOTELS_PATH) as f:
    HOTEL_DATA = json.load(f)


@dataclass
class BookingState:
    """Tracks the current booking session."""
    current_city: str | None = None
    selected_hotel_id: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    guests: int = 1
    booked: bool = False


AGENT_VOICES = {
    "Sophie": "Eva",
    "Sydney": "Sydney",
}

# ---------------------------------------------------------------------------
# Shared prompt fragments
# ---------------------------------------------------------------------------

_CITY_KEYS = list(HOTEL_DATA["cities"].keys())
_AVAILABLE_CITIES = ", ".join(
    city_data["name"] for city_data in HOTEL_DATA["cities"].values()
)

# Build city knowledge block once
_CITY_KNOWLEDGE = ""
for _key, _city in HOTEL_DATA["cities"].items():
    _CITY_KNOWLEDGE += f"\n--- {_city['name']} ({_city['country']}) ---\n"
    _CITY_KNOWLEDGE += f"Description: {_city['description']}\n"
    _CITY_KNOWLEDGE += "Current events:\n"
    for _ev in _city.get("events", []):
        _CITY_KNOWLEDGE += f"  - {_ev}\n"
    _CITY_KNOWLEDGE += "Top attractions:\n"
    for _att in _city.get("attractions", []):
        _CITY_KNOWLEDGE += f"  - {_att}\n"


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_BASE_PROMPT_TEMPLATE = (_PROMPTS_DIR / "base.txt").read_text()
_PHASE1_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase1.txt").read_text()
_PHASE2_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase2.txt").read_text()
_PHASE3_PROMPT_TEMPLATE = (_PROMPTS_DIR / "phase3.txt").read_text()


def _base_prompt(agent_name: str) -> str:
    """Personality + style rules shared across all phases."""
    return _BASE_PROMPT_TEMPLATE.format(agent_name=agent_name)


def get_phase1_prompt(agent_name: str) -> str:
    """Phase 1: City selection — caller hasn't picked a city yet, or we're searching."""
    return _base_prompt(agent_name) + _PHASE1_PROMPT_TEMPLATE.format(
        available_cities=_AVAILABLE_CITIES,
        city_keys=', '.join(_CITY_KEYS),
        city_knowledge=_CITY_KNOWLEDGE,
    )


def get_phase2_prompt(agent_name: str, city_name: str, hotel_summaries: list[dict]) -> str:
    """Phase 2: Hotel selection — hotels are loaded, caller needs to pick one."""
    hotel_list = ""
    for h in hotel_summaries:
        hotel_list += f"\n- {h['name']} (ID: {h['id']}, {h['stars']}\u2605) \u2014 {h['description']}. Price range: {h['price_range']}. Phone: {h['phone']}"

    hotel_names = ", ".join(h["name"] for h in hotel_summaries)
    hotel_ids = ", ".join(h["id"] for h in hotel_summaries)

    return _base_prompt(agent_name) + _PHASE2_PROMPT_TEMPLATE.format(
        city_name=city_name,
        hotel_list=hotel_list,
        hotel_names=hotel_names,
        hotel_ids=hotel_ids,
        available_cities=_AVAILABLE_CITIES,
        city_keys=', '.join(_CITY_KEYS),
        city_knowledge=_CITY_KNOWLEDGE,
    )


def get_phase3_prompt(agent_name: str, hotel_name: str, room_summaries: list[dict], hotel_phone: str) -> str:
    """Phase 3: Room selection & booking — rooms are loaded, caller needs to pick one."""
    room_list = ""
    for r in room_summaries:
        room_list += f"\n- {r['type']}: ${r['price_per_night']}/night \u2014 {r['description']} (max {r['max_guests']} guests)"

    return _base_prompt(agent_name) + _PHASE3_PROMPT_TEMPLATE.format(
        hotel_name=hotel_name,
        hotel_phone=hotel_phone,
        room_list=room_list,
        available_cities=_AVAILABLE_CITIES,
        city_keys=', '.join(_CITY_KEYS),
    )


def build_tools() -> list[gradbot.ToolDef]:
    city_keys = ", ".join(HOTEL_DATA["cities"].keys())
    all_hotel_ids = []
    for city_data in HOTEL_DATA["cities"].values():
        for hotel in city_data["hotels"]:
            all_hotel_ids.append(hotel["id"])
    hotel_ids_str = ", ".join(all_hotel_ids)

    return [
        gradbot.ToolDef(
            name="search_hotels",
            description=f"Search for available hotels in a city. Takes some time to query the system. Keep chatting with the caller about the destination while waiting for results! Available cities: {city_keys}",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": f"The city to search in. One of: {city_keys}"
                    }
                },
                "required": ["city"]
            }),
        ),
        gradbot.ToolDef(
            name="get_hotel_details",
            description=f"Get detailed room information and prices for a specific hotel. You MUST call this before you can talk about rooms or prices. Takes some time - keep chatting with the caller! Known hotel IDs: {hotel_ids_str}",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "hotel_id": {
                        "type": "string",
                        "description": f"The hotel ID to look up. One of: {hotel_ids_str}"
                    }
                },
                "required": ["hotel_id"]
            }),
        ),
        gradbot.ToolDef(
            name="book_room",
            description="Book a room at a hotel. Use this when the caller has decided on a hotel, room type, dates, and number of guests.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "hotel_id": {
                        "type": "string",
                        "description": "The hotel ID"
                    },
                    "room_type": {
                        "type": "string",
                        "description": "The room type (e.g. 'Classic Room', 'Deluxe Suite')"
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
                "required": ["hotel_id", "room_type", "check_in", "check_out", "guests", "guest_name"]
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

    async def handle_search_hotels(city_key: str, tool_handle, input_handle, websocket: WebSocket):
        """Search hotels with a realistic delay."""
        city_names = {k: v["name"] for k, v in HOTEL_DATA["cities"].items()}
        city_name = city_names.get(city_key, city_key)

        await websocket.send_json({
            "type": "tool_started",
            "tool": "search_hotels",
            "message": f"Searching hotels in {city_name}...",
        })

        delay = random.uniform(8, 15)
        logger.info("Searching hotels in %s (delay: %.1fs)", city_key, delay)
        await asyncio.sleep(delay)

        city_data = HOTEL_DATA["cities"].get(city_key)
        if not city_data:
            available = ", ".join(HOTEL_DATA["cities"].keys())
            await tool_handle.send(json.dumps({
                "success": False,
                "message": f"City '{city_key}' not found. Available cities: {available}.",
            }))
            return

        state.current_city = city_key

        hotel_summaries = []
        hotels_for_frontend = []
        for hotel in city_data["hotels"]:
            price_range = [r["price_per_night"] for r in hotel["rooms"]]
            summary = {
                "id": hotel["id"],
                "name": hotel["name"],
                "stars": hotel["stars"],
                "description": hotel["description"],
                "phone": compact_phone(hotel.get("phone", "")),
                "price_range": f"${min(price_range)} - ${max(price_range)} per night",
                "amenities": hotel["amenities"],
            }
            hotel_summaries.append(summary)
            hotels_for_frontend.append({
                **summary,
                "phone": hotel.get("phone", ""),
                "image_url": hotel["image_url"],
            })

        await websocket.send_json({
            "type": "hotel_results",
            "city": city_data["name"],
            "city_image": city_data["image_url"],
            "hotels": hotels_for_frontend,
        })

        # Swap to phase 2 prompt
        phase2 = get_phase2_prompt(agent_name, city_data["name"], hotel_summaries)
        await input_handle.send_config(make_config(phase2))
        logger.info("Switched to phase 2 prompt for %s", city_data["name"])

        await tool_handle.send(json.dumps({
            "success": True,
            "city": city_data["name"],
            "country": city_data["country"],
            "hotels": hotel_summaries,
            "message": f"Hotels for {city_data['name']} are now loaded. Present the options highlighting star ratings and key features. Ask which one interests them!"
                f"\n\nREMINDER: You do NOT have room details yet. The INSTANT the caller picks a hotel, call get_hotel_details. Do NOT make up rooms or prices.",
        }))

        logger.info("Search complete for %s: %d hotels", city_key, len(hotel_summaries))

    async def handle_hotel_details(hotel_id: str, tool_handle, input_handle, websocket: WebSocket):
        """Get hotel details with a realistic delay."""
        hotel_name_preview = hotel_id
        for city_data in HOTEL_DATA["cities"].values():
            for h in city_data["hotels"]:
                if h["id"] == hotel_id:
                    hotel_name_preview = h["name"]
                    break

        await websocket.send_json({
            "type": "tool_started",
            "tool": "get_hotel_details",
            "message": f"Loading rooms for {hotel_name_preview}...",
        })

        delay = random.uniform(4, 8)
        logger.info("Loading details for %s (delay: %.1fs)", hotel_id, delay)
        await asyncio.sleep(delay)

        hotel = None
        for city_data in HOTEL_DATA["cities"].values():
            for h in city_data["hotels"]:
                if h["id"] == hotel_id:
                    hotel = h
                    break
            if hotel:
                break

        if not hotel:
            await tool_handle.send(json.dumps({
                "success": False,
                "message": f"Hotel '{hotel_id}' not found.",
            }))
            return

        state.selected_hotel_id = hotel_id

        room_summaries = []
        rooms_for_frontend = []
        for room in hotel["rooms"]:
            room_summaries.append({
                "type": room["type"],
                "price_per_night": room["price_per_night"],
                "description": room["description"],
                "max_guests": room["max_guests"],
            })
            rooms_for_frontend.append({
                **room,
            })

        await websocket.send_json({
            "type": "hotel_details",
            "hotel": {
                "id": hotel["id"],
                "name": hotel["name"],
                "stars": hotel["stars"],
                "phone": hotel.get("phone", ""),
                "description": hotel["description"],
                "image_url": hotel["image_url"],
                "amenities": hotel["amenities"],
                "rooms": rooms_for_frontend,
            },
        })

        # Swap to phase 3 prompt
        phone = compact_phone(hotel.get("phone", ""))
        phase3 = get_phase3_prompt(agent_name, hotel["name"], room_summaries, phone)
        await input_handle.send_config(make_config(phase3))
        logger.info("Switched to phase 3 prompt for %s", hotel["name"])

        await tool_handle.send(json.dumps({
            "success": True,
            "hotel_name": hotel["name"],
            "stars": hotel["stars"],
            "phone": phone,
            "amenities": hotel["amenities"],
            "rooms": room_summaries,
            "message": f"Room details for {hotel['name']} are ready. Present each room type with its price per night. Help them choose!",
        }))

        logger.info("Details loaded for %s", hotel_id)

    async def handle_book_room(args: dict, tool_handle, websocket: WebSocket):
        """Process a booking."""
        hotel_id = args["hotel_id"]
        room_type = args["room_type"]

        hotel = None
        room = None
        for city_data in HOTEL_DATA["cities"].values():
            for h in city_data["hotels"]:
                if h["id"] == hotel_id:
                    hotel = h
                    for r in h["rooms"]:
                        if r["type"].lower() == room_type.lower():
                            room = r
                            break
                    break
            if hotel:
                break

        if not hotel or not room:
            await tool_handle.send(json.dumps({
                "success": False,
                "message": "Could not find the specified hotel or room type.",
            }))
            return

        state.booked = True
        confirmation = f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{random.randint(1000, 9999)}"

        booking_info = {
            "confirmation_number": confirmation,
            "hotel": hotel["name"],
            "room": room["type"],
            "price_per_night": room["price_per_night"],
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
            city = args.get("city", "").lower().strip()
            await handle_search_hotels(city, tool_handle, input_handle, websocket)

        elif tool_name == "get_hotel_details":
            hotel_id = args.get("hotel_id", "")
            await handle_hotel_details(hotel_id, tool_handle, input_handle, websocket)

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
        logger.info("Starting hotel reservation chat with %s (voice: %s, padding_bonus: %s)",
                     agent_name, voice_key, padding_bonus)

        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_phase1_prompt(agent_name),
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
