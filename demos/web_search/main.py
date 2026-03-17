"""
Web Search Demo - Voice-powered web search and weather assistant

A voice agent that searches the web via Linkup and fetches weather via Open-Meteo.
Results appear in a side panel while the AI discusses and summarizes findings.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from linkup import LinkupClient

import gradbot
from gradbot.fastapi import websocket_chat_handler

gradbot.init_logging()
logger = logging.getLogger(__name__)

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

linkup_client = LinkupClient(
    api_key=_YAML_CFG.get("linkup", {}).get("api_key") or os.environ.get("LINKUP_API_KEY", ""),
)

# WMO weather codes to human-readable descriptions
WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


AGENT_VOICES = {
    "Alex": "Eva",
    "Nova": "Sydney",
}


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SYSTEM_PROMPT_TEMPLATE = (_PROMPTS_DIR / "system.txt").read_text()


def get_prompt(agent_name: str) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(agent_name=agent_name)


def build_tools() -> list[gradbot.ToolDef]:
    return [
        gradbot.ToolDef(
            name="web_search",
            description="Search the web for information, news, current events, or any topic. Use this whenever the user asks about anything that isn't weather. Keep chatting while waiting for results!",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    }
                },
                "required": ["query"]
            }),
        ),
        gradbot.ToolDef(
            name="get_weather",
            description="Get the current weather for a city. Returns temperature, wind speed, and conditions.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name (e.g., 'Paris', 'New York', 'Tokyo')",
                    },
                    "country_code": {
                        "type": "string",
                        "description": "Optional ISO 3166-1 alpha-2 country code to disambiguate (e.g., 'FR', 'US', 'JP')",
                    },
                },
                "required": ["city"],
            }),
        ),
    ]


_TOOLS = build_tools()


async def do_search(query: str) -> dict:
    """Run Linkup search in a thread pool."""
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: linkup_client.search(
                query=query,
                depth="standard",
                output_type="sourcedAnswer",
            ),
        )
        sources = [
            {"title": s.name, "href": s.url, "body": s.snippet}
            for s in (result.sources or [])
        ]
        logger.info("Linkup search '%s': %d sources", query, len(sources))
        return {
            "answer": result.answer or "",
            "sources": sources,
        }
    except Exception as e:
        logger.error("Linkup search '%s' failed: %s", query, e)
        return {"answer": "", "sources": []}


async def fetch_weather(city: str, country_code: str | None = None) -> dict:
    """Fetch current weather using Open-Meteo (no API key needed)."""
    params = {"name": city, "count": 1, "language": "en", "format": "json"}
    if country_code:
        params["country"] = country_code
    geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?{urllib.parse.urlencode(params)}"

    loop = asyncio.get_running_loop()
    try:
        geo_response = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(geocode_url, timeout=10).read().decode(),
        )
        geo_data = json.loads(geo_response)
    except Exception as e:
        return {"error": f"Failed to geocode city '{city}': {e}"}

    if "results" not in geo_data or len(geo_data["results"]) == 0:
        return {"error": f"City '{city}' not found"}

    location = geo_data["results"][0]
    lat = location["latitude"]
    lon = location["longitude"]
    resolved_name = location.get("name", city)
    country = location.get("country", "")

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&temperature_unit=celsius"
        f"&wind_speed_unit=kmh"
    )

    try:
        weather_response = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(weather_url, timeout=10).read().decode(),
        )
        weather_data = json.loads(weather_response)
    except Exception as e:
        return {"error": f"Failed to fetch weather: {e}"}

    current = weather_data.get("current_weather", {})
    weather_code = current.get("weathercode", -1)
    description = WMO_CODES.get(weather_code, "unknown conditions")

    return {
        "city": resolved_name,
        "country": country,
        "temperature_c": current.get("temperature"),
        "wind_speed_kmh": current.get("windspeed"),
        "wind_direction": current.get("winddirection"),
        "conditions": description,
        "is_day": current.get("is_day", 1) == 1,
    }


async def handle_web_search(query: str, tool_handle, websocket: WebSocket):
    """Search the web and send results to frontend + LLM."""
    await websocket.send_json({
        "type": "tool_started",
        "message": f"Searching for \"{query}\"...",
    })

    try:
        result = await do_search(query)
    except Exception as e:
        logger.error("Search error: %s", e)
        await websocket.send_json({
            "type": "search_results",
            "query": query,
            "results": [],
        })
        await tool_handle.send(json.dumps({
            "success": False,
            "message": f"Search failed: {e}",
        }))
        return

    await websocket.send_json({
        "type": "search_results",
        "query": query,
        "results": result["sources"],
    })

    await tool_handle.send(json.dumps({
        "success": True,
        "query": query,
        "answer": result["answer"],
        "results": result["sources"],
        "message": f"Found {len(result['sources'])} results for \"{query}\". Here is a sourced answer: {result['answer']}. Discuss the findings with the user. Be concise.",
    }))

    logger.info("Search complete for '%s': %d results", query, len(result["sources"]))


async def handle_weather(args: dict, tool_handle, websocket: WebSocket):
    """Fetch weather and send to frontend + LLM."""
    city = args.get("city", "")
    country_code = args.get("country_code")

    await websocket.send_json({
        "type": "tool_started",
        "message": f"Checking weather in {city}...",
    })

    try:
        result = await fetch_weather(city, country_code)
    except Exception as e:
        logger.error("Weather error: %s", e)
        result = {"error": f"Failed to fetch weather: {e}"}

    await websocket.send_json({"type": "weather_result", **result})
    await tool_handle.send(json.dumps(result))


app = FastAPI(title="Web Search Demo")


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):

    async def on_start(msg: dict) -> gradbot.SessionConfig:
        agent_name = msg.get("agent", "Alex")
        padding_bonus = float(msg.get("padding_bonus", 0.0))
        voice_key = AGENT_VOICES.get(agent_name, "Eva")
        logger.info("Starting web search chat with %s (voice: %s, padding_bonus: %s)",
                     agent_name, voice_key, padding_bonus)

        voice = gradbot.flagship_voice(voice_key)

        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_prompt(agent_name),
            language=gradbot.Lang.En,
            tools=_TOOLS,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="en",
                assistant_speaks_first=True,
            ),
        )

    async def on_tool(tool_call, tool_handle, input_handle, websocket):
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        logger.info("Tool call: %s - %s", tool_name, args)

        if tool_name == "web_search":
            query = args.get("query", "")
            await handle_web_search(query, tool_handle, websocket)
        elif tool_name == "get_weather":
            await handle_weather(args, tool_handle, websocket)
        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")

    await websocket_chat_handler(
        websocket,
        on_start=on_start,
        on_tool_call=on_tool,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


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
