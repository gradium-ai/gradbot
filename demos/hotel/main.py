"""
Hotel Reservation Demo - Voice AI hotel booking agent

A voice agent that helps callers search for and book hotels in Paris, Bali, and Dubai.
Searches are deferred (10-20s random delay) to demonstrate chit-chat while waiting.

Run with: uvicorn main:app --reload
"""

import asyncio
import os
import json
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pygradbot

pygradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)


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
    pending_tasks: list[asyncio.Task] = field(default_factory=list)


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


def _base_prompt(agent_name: str) -> str:
    """Personality + style rules shared across all phases."""
    return f"""You are {agent_name}, a warm and knowledgeable hotel reservation agent at Wanderlust Travel.

You help callers find and book the perfect hotel. You're friendly, enthusiastic about travel,
and love sharing destination tips while helping with reservations.

YOUR PERSONALITY:
- Warm, professional, and genuinely enthusiastic about travel
- You love sharing fun facts and tips about destinations
- You make callers feel like they're talking to a well-traveled friend
- You're patient and helpful, never pushy
- You proactively SUGGEST destinations if the caller is undecided

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* - just speak naturally
- Be conversational and natural, like a real phone call

NEVER FABRICATE DATA:
- NEVER make up or guess hotel names, room names, room types, prices, or availability.
- ONLY present information that came back from a tool call result.
- If you called a tool and the result has NOT arrived yet, you DO NOT have the data. Period.
- NEVER pretend a tool call result has arrived when it hasn't. If you haven't seen the result, say you're still waiting.
- Talking about rooms or prices before the tool result is back is FABRICATION and is absolutely forbidden.

PHONE NUMBERS:
- When asked, output the number as a single block with no spaces, e.g. "Their number is +33142689100."
- Do NOT spell out digits one by one. Do NOT add spaces or dashes. Just output the raw number.

WHILE WAITING FOR TOOL RESULTS:
- Do NOT ask questions — the results arrive in 10-20 seconds and the caller won't have time to answer.
- Instead, SHARE information: fun facts, tips, destination highlights.
- Save your questions for AFTER the results have arrived.
"""


def get_phase1_prompt(agent_name: str) -> str:
    """Phase 1: City selection — caller hasn't picked a city yet, or we're searching."""
    return _base_prompt(agent_name) + f"""
CURRENT PHASE: CITY SELECTION
Available destinations: {_AVAILABLE_CITIES}
(City keys for tool calls: {', '.join(_CITY_KEYS)})

YOUR ONE JOB RIGHT NOW: Help the caller pick a destination and search for hotels.

RULE: The INSTANT the caller mentions a city or destination, you MUST call search_hotels.
Do NOT ask more questions first. Do NOT share facts first. Call the tool FIRST, THEN talk.
If the caller switches cities, call search_hotels again for the new city. No limit on calls.

NEVER say "I'm searching" or "let me look that up" without ACTUALLY calling search_hotels.
Saying it without doing it is the worst mistake you can make.

After calling the tool, share fun facts about the destination while we wait for results.

CITY KNOWLEDGE — SHARE THIS WHILE WAITING:
{_CITY_KNOWLEDGE}

Start by greeting the caller warmly and asking how you can help with their travel plans.
If they seem unsure, suggest some destinations!
"""


def get_phase2_prompt(agent_name: str, city_name: str, hotel_summaries: list[dict]) -> str:
    """Phase 2: Hotel selection — hotels are loaded, caller needs to pick one."""
    hotel_list = ""
    for h in hotel_summaries:
        hotel_list += f"\n- {h['name']} (ID: {h['id']}, {h['stars']}★) — {h['description']}. Price range: {h['price_range']}. Phone: {h['phone']}"

    hotel_names = ", ".join(h["name"] for h in hotel_summaries)
    hotel_ids = ", ".join(h["id"] for h in hotel_summaries)

    return _base_prompt(agent_name) + f"""
CURRENT PHASE: HOTEL SELECTION
You have loaded hotels for {city_name}. Here are the options:
{hotel_list}

YOUR ONE JOB RIGHT NOW: Help the caller pick a hotel from the list above.
Present the options, then wait for them to show interest in one.

⚠️ MANDATORY — YOUR #1 RULE IN THIS PHASE:
The INSTANT the caller mentions ANY hotel by name, says "the first one", "that one",
"tell me more about...", "how much is...", asks about rooms, prices, or availability,
or shows ANY interest in a specific hotel — you MUST IMMEDIATELY call get_hotel_details.

You do NOT have room types or prices yet. You only have a rough price range.
You CANNOT answer questions about rooms or specific prices without calling get_hotel_details.
If you try to answer without calling the tool, you WILL fabricate data. DO NOT DO THIS.

Call get_hotel_details FIRST, then chat while it loads. This is not optional.

Hotel IDs for tool calls: {hotel_ids}
Hotel names: {hotel_names}

If the caller wants to explore a different city, call search_hotels immediately.
Available cities: {_AVAILABLE_CITIES} (keys: {', '.join(_CITY_KEYS)})

CITY KNOWLEDGE — SHARE THIS WHILE WAITING FOR TOOL RESULTS:
{_CITY_KNOWLEDGE}
"""


def get_phase3_prompt(agent_name: str, hotel_name: str, room_summaries: list[dict], hotel_phone: str) -> str:
    """Phase 3: Room selection & booking — rooms are loaded, caller needs to pick one."""
    room_list = ""
    for r in room_summaries:
        room_list += f"\n- {r['type']}: ${r['price_per_night']}/night — {r['description']} (max {r['max_guests']} guests)"

    return _base_prompt(agent_name) + f"""
CURRENT PHASE: ROOM SELECTION & BOOKING
The caller is looking at {hotel_name}. Phone: {hotel_phone}

Here are the available rooms:
{room_list}

YOUR ONE JOB RIGHT NOW: Help the caller pick a room and complete the booking.
Present the room options with prices. Help them choose based on preferences and budget.

To complete a booking you need: room type, check-in date, check-out date, number of guests, and guest name.
Ask for any missing details, then call book_room.

ONLY use the room names and prices listed above. NEVER invent room types or prices.

After booking, congratulate them and summarize the reservation with the confirmation number.

If the caller wants to look at a different hotel, call get_hotel_details immediately.
If the caller wants to explore a different city, call search_hotels immediately.
Available cities: {_AVAILABLE_CITIES} (keys: {', '.join(_CITY_KEYS)})
"""


def build_tools() -> list[pygradbot.ToolDef]:
    city_keys = ", ".join(HOTEL_DATA["cities"].keys())
    all_hotel_ids = []
    for city_data in HOTEL_DATA["cities"].values():
        for hotel in city_data["hotels"]:
            all_hotel_ids.append(hotel["id"])
    hotel_ids_str = ", ".join(all_hotel_ids)

    return [
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
    print("Starting Hotel Reservation Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Hotel Reservation Demo", lifespan=lifespan)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    state = BookingState()

    try:
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        agent_name = start_msg.get("agent", "Sophie")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        voice_key = AGENT_VOICES.get(agent_name, "Eva")
        print(f"Starting hotel reservation chat with {agent_name} (voice: {voice_key}, padding_bonus: {padding_bonus})")

        voice = pygradbot.flagship_voice(voice_key)
        tools = build_tools()

        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_phase1_prompt(agent_name),
            language=pygradbot.Lang.En,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="en",
            ),
        )

        input_handle, output_handle = await pygradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.Pcm if USE_PCM else pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        def make_config(instructions: str) -> pygradbot.SessionConfig:
            return pygradbot.SessionConfig(
                voice_id=voice.voice_id,
                instructions=instructions,
                language=pygradbot.Lang.En,
                tools=tools,
                **merge_overrides(_OVERRIDES,
                    flush_duration_s=FLUSH_FOR_S,
                    padding_bonus=padding_bonus,
                    rewrite_rules="en",
                ),
            )

        async def handle_search_hotels(city_key: str, tool_handle):
            """Search hotels with a realistic delay."""
            city_names = {k: v["name"] for k, v in HOTEL_DATA["cities"].items()}
            city_name = city_names.get(city_key, city_key)

            # Notify frontend of the tool call
            await websocket.send_json({
                "type": "tool_started",
                "tool": "search_hotels",
                "message": f"Searching hotels in {city_name}...",
            })

            delay = random.uniform(8, 15)
            print(f"Searching hotels in {city_key} (delay: {delay:.1f}s)")
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

            # Build hotel summaries for the LLM (include phone numbers)
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

            # Send to frontend
            await websocket.send_json({
                "type": "hotel_results",
                "city": city_data["name"],
                "city_image": city_data["image_url"],
                "hotels": hotels_for_frontend,
            })

            # Swap to phase 2 prompt
            phase2 = get_phase2_prompt(agent_name, city_data["name"], hotel_summaries)
            await input_handle.send_config(make_config(phase2))
            print(f"Switched to phase 2 prompt for {city_data['name']}")

            # Send tool result to LLM
            hotel_names_str = ", ".join(h["name"] for h in hotel_summaries)
            await tool_handle.send(json.dumps({
                "success": True,
                "city": city_data["name"],
                "country": city_data["country"],
                "hotels": hotel_summaries,
                "message": f"Hotels for {city_data['name']} are now loaded. Present the options highlighting star ratings and key features. Ask which one interests them!"
                    f"\n\nREMINDER: You do NOT have room details yet. The INSTANT the caller picks a hotel, call get_hotel_details. Do NOT make up rooms or prices.",
            }))

            print(f"Search complete for {city_key}: {len(hotel_summaries)} hotels")

        async def handle_hotel_details(hotel_id: str, tool_handle):
            """Get hotel details with a realistic delay."""
            # Find hotel name for the notification
            hotel_name_preview = hotel_id
            for city_data in HOTEL_DATA["cities"].values():
                for h in city_data["hotels"]:
                    if h["id"] == hotel_id:
                        hotel_name_preview = h["name"]
                        break

            # Notify frontend of the tool call
            await websocket.send_json({
                "type": "tool_started",
                "tool": "get_hotel_details",
                "message": f"Loading rooms for {hotel_name_preview}...",
            })

            delay = random.uniform(4, 8)
            print(f"Loading details for {hotel_id} (delay: {delay:.1f}s)")
            await asyncio.sleep(delay)

            # Find the hotel
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

            # Build room info for LLM
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

            # Send to frontend
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
            print(f"Switched to phase 3 prompt for {hotel['name']}")

            # Send tool result to LLM
            await tool_handle.send(json.dumps({
                "success": True,
                "hotel_name": hotel["name"],
                "stars": hotel["stars"],
                "phone": phone,
                "amenities": hotel["amenities"],
                "rooms": room_summaries,
                "message": f"Room details for {hotel['name']} are ready. Present each room type with its price per night. Help them choose!",
            }))

            print(f"Details loaded for {hotel_id}")

        async def handle_book_room(args: dict, tool_handle):
            """Process a booking."""
            hotel_id = args["hotel_id"]
            room_type = args["room_type"]

            # Find hotel and room
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

            # Send to frontend
            await websocket.send_json({
                "type": "booking_confirmed",
                "booking": booking_info,
            })

            # Send to LLM
            await tool_handle.send(json.dumps({
                "success": True,
                "booking": booking_info,
                "message": f"Booking confirmed! Confirmation number: {confirmation}. Congratulate the caller and summarize their reservation details.",
            }))

            print(f"Booking confirmed: {confirmation}")

        async def handle_tool_call(tool_call, tool_handle):
            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)
            print(f"Tool call: {tool_name} - {args}")

            if tool_name == "search_hotels":
                city = args.get("city", "").lower().strip()
                task = asyncio.create_task(handle_search_hotels(city, tool_handle))
                state.pending_tasks.append(task)
                task.add_done_callback(lambda t: state.pending_tasks.remove(t) if t in state.pending_tasks else None)

            elif tool_name == "get_hotel_details":
                hotel_id = args.get("hotel_id", "")
                task = asyncio.create_task(handle_hotel_details(hotel_id, tool_handle))
                state.pending_tasks.append(task)
                task.add_done_callback(lambda t: state.pending_tasks.remove(t) if t in state.pending_tasks else None)

            elif tool_name == "book_room":
                await handle_book_room(args, tool_handle)

            else:
                await tool_handle.send_error(f"Unknown tool: {tool_name}")

        async def process_output():
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        break

                    if msg.msg_type == "audio":
                        await websocket.send_json({
                            "type": "audio_timing",
                            "start_s": msg.start_s,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                            "interrupted": msg.interrupted,
                        })
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(handle_tool_call(msg.tool_call, msg.tool_call_handle))

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    import traceback
                    traceback.print_exc()
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e) if DEBUG else "An error occurred during the session",
                        })
                    except:
                        pass
                    break

        async def receive_audio():
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        if data.get("type") == "stop":
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

        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e) if DEBUG else "An error occurred while starting the session",
            })
        except:
            pass
    finally:
        for task in state.pending_tasks:
            if not task.done():
                task.cancel()
        state.pending_tasks.clear()
        try:
            await websocket.close()
        except:
            pass


@app.get("/api/audio-config")
async def audio_config():
    return JSONResponse(content={"pcm": USE_PCM})

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")


@app.get("/")
async def index():
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
