"""
News & Weather Demo - AI assistant that fetches live weather and news headlines

A FastAPI backend that exposes:
- GET /api/voices - list available flagship voices
- WebSocket /ws/chat - real-time voice conversation with weather and news tools

The AI can:
1. Fetch current weather for any city (via Open-Meteo, no API key)
2. Read latest news headlines from RSS feeds (BBC, Guardian, Le Monde, Ars Technica)
3. Switch between different voice personas

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

import gradbot
from gradbot.fastapi import websocket_chat_handler
import feedparser

# Initialize Rust logging (outputs to stderr)
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

RSS_FEEDS = {
    "bbc": {
        "name": "BBC News",
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
    },
    "guardian": {
        "name": "The Guardian",
        "url": "https://www.theguardian.com/world/rss",
    },
    "le_monde": {
        "name": "Le Monde",
        "url": "https://www.lemonde.fr/rss/une.xml",
    },
    "ars_technica": {
        "name": "Ars Technica",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
    },
}


def lang_to_code(lang: gradbot.Lang) -> str:
    """Convert Lang enum to language code."""
    if lang == gradbot.Lang.En:
        return "en"
    elif lang == gradbot.Lang.Fr:
        return "fr"
    elif lang == gradbot.Lang.De:
        return "de"
    elif lang == gradbot.Lang.Es:
        return "es"
    elif lang == gradbot.Lang.Pt:
        return "pt"
    return "en"


def build_voice_tools() -> list[gradbot.ToolDef]:
    """Build tool definitions for each voice."""
    tools = []
    for voice in gradbot.flagship_voices():
        tool = gradbot.ToolDef(
            name=f"switch_to_{voice.name.lower()}",
            description=f"Switch to {voice.name}'s voice. {voice.description}",
            parameters_json=json.dumps(
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            ),
        )
        tools.append(tool)
    return tools


def build_weather_tool() -> gradbot.ToolDef:
    """Build tool definition for weather lookup."""
    return gradbot.ToolDef(
        name="get_weather",
        description="Get the current weather for a city. Returns temperature, wind speed, and conditions.",
        parameters_json=json.dumps(
            {
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
            }
        ),
    )


def build_news_tool() -> gradbot.ToolDef:
    """Build tool definition for news fetching."""
    source_descriptions = ", ".join(
        f'"{key}" ({info["name"]})' for key, info in RSS_FEEDS.items()
    )
    return gradbot.ToolDef(
        name="get_news",
        description=f"Fetch the latest news headlines from an RSS feed. Available sources: {source_descriptions}.",
        parameters_json=json.dumps(
            {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": list(RSS_FEEDS.keys()),
                        "description": "The news source to fetch from",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of headlines to fetch (default 5, max 10)",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["source"],
            }
        ),
    )


async def fetch_weather(city: str, country_code: str | None = None) -> dict:
    """Fetch current weather using Open-Meteo (no API key needed)."""
    # Step 1: Geocode the city name
    params = {"name": city, "count": 1, "language": "en", "format": "json"}
    if country_code:
        params["country"] = country_code
    geocode_url = f"https://geocoding-api.open-meteo.com/v1/search?{urllib.parse.urlencode(params)}"

    loop = asyncio.get_event_loop()
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

    # Step 2: Fetch current weather
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


async def fetch_news(source: str, count: int = 5) -> dict:
    """Fetch latest news headlines from an RSS feed."""
    if source not in RSS_FEEDS:
        return {"error": f"Unknown source '{source}'. Available: {', '.join(RSS_FEEDS.keys())}"}

    feed_info = RSS_FEEDS[source]
    feed_url = feed_info["url"]

    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(feed_url, timeout=15).read(),
        )
        feed = feedparser.parse(raw)
    except Exception as e:
        return {"error": f"Failed to fetch {feed_info['name']} feed: {e}"}

    headlines = []
    for entry in feed.entries[:count]:
        headline = {
            "title": entry.get("title", "No title"),
        }
        summary = entry.get("summary", entry.get("description", ""))
        if summary:
            # Strip HTML tags simply
            import re

            summary = re.sub(r"<[^>]+>", "", summary).strip()
            if len(summary) > 200:
                summary = summary[:200] + "..."
            headline["summary"] = summary
        headlines.append(headline)

    return {
        "source": feed_info["name"],
        "headline_count": len(headlines),
        "headlines": headlines,
    }


def get_system_prompt(current_voice_name: str) -> str:
    """Build the system prompt for the news & weather demo."""
    voice = gradbot.flagship_voice(current_voice_name)

    sources_list = ", ".join(info["name"] for info in RSS_FEEDS.values())

    return f"""You are {voice.name}, a friendly AI assistant who helps people stay informed about the weather and the latest news.

{voice.description}

YOUR CAPABILITIES:
1. You can look up the current weather for any city in the world using the get_weather tool
2. You can fetch the latest news headlines from: {sources_list}
3. You can switch between different voice personas using switch_to_* tools

NEVER FABRICATE DATA:
- NEVER make up weather information, temperatures, forecasts, or conditions.
- NEVER invent news headlines, article content, or summaries.
- ONLY report information that came back from a tool call result.
- If you called a tool and the result has NOT arrived yet, you DO NOT have the data.
- While waiting for results, make small talk — but NEVER guess at what the results might be.

WEATHER:
- When the user asks about the weather, call get_weather IMMEDIATELY
- Report temperature in Celsius, mention wind and conditions
- You can look up weather for any city - just ask if you're unsure which city they mean
- If they don't specify a city, ask them which city they'd like
- While waiting for weather results, chat about the city or ask what their plans are — do NOT guess the weather

NEWS:
- When the user asks for news, call get_news IMMEDIATELY
- Read out the most interesting headlines with brief summaries
- Don't just list every headline mechanically - pick the most interesting ones and present them conversationally
- Available sources: {sources_list}
- If the user doesn't specify a source, pick one that seems appropriate or ask their preference
- For French news, use Le Monde. For tech news, use Ars Technica.
- While waiting for news results, ask what topics interest them — do NOT make up headlines

VOICE SWITCHING:
- Feel free to switch voices for fun or when the user asks
- Each voice has a different personality - embrace it!

CONVERSATION STYLE:
- Keep responses conversational and natural
- Don't read out raw data - interpret it naturally (e.g., "It's a warm 25 degrees with clear skies" not "Temperature: 25C, weathercode: 0")
- For news, be engaging - add brief commentary or ask if the user wants to hear more about a topic
- Feel free to suggest looking up weather or news if the conversation is idle

Start by greeting the user and asking what they'd like to know - weather, news, or just a chat!
"""


app = FastAPI(title="News & Weather Demo")


@app.get("/api/voices")
async def list_voices():
    """Return list of available flagship voices with full details."""
    voices = [
        {
            "name": v.name,
            "voice_id": v.voice_id,
            "language": lang_to_code(v.language),
            "country": v.country.code(),
            "country_name": str(v.country),
            "gender": str(v.gender),
            "description": v.description,
        }
        for v in gradbot.flagship_voices()
    ]
    return JSONResponse(content={"voices": voices})


@app.get("/api/sources")
async def list_sources():
    """Return available news sources."""
    return JSONResponse(
        content={
            "sources": [
                {"id": k, "name": v["name"]} for k, v in RSS_FEEDS.items()
            ]
        }
    )


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for voice chat with weather and news tools.

    Protocol:
    - Client sends JSON: {"type": "start", "voice_name": "Emma"}
    - Client sends binary: audio data (Ogg Opus)
    - Server sends JSON: {"type": "transcript", "text": "...", "is_user": true/false}
    - Server sends JSON: {"type": "voice_change", "voice_name": "..."}
    - Server sends JSON: {"type": "weather_result", ...}
    - Server sends JSON: {"type": "news_result", ...}
    - Server sends JSON: {"type": "event", "event": "..."}
    - Server sends binary: audio data
    - Client sends JSON: {"type": "stop"} to end
    """
    # Build tools once (shared across the session, voice tools don't change)
    voice_tools = build_voice_tools()
    weather_tool = build_weather_tool()
    news_tool = build_news_tool()
    tools = voice_tools + [weather_tool, news_tool]
    logger.info("Built %d tools: %d voice + weather + news", len(tools), len(voice_tools))

    # Mutable per-session state
    current_voice = None

    def _make_config(voice_name: str) -> gradbot.SessionConfig:
        nonlocal current_voice
        voice = gradbot.flagship_voice(voice_name)
        current_voice = voice_name
        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(voice_name),
            language=voice.language,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=voice.language.rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

    def on_start(msg: dict) -> gradbot.SessionConfig:
        voice_name = msg.get("voice_name", "Emma")
        logger.info("Starting news & weather chat with voice=%s", voice_name)
        return _make_config(voice_name)

    async def on_tool(tool_call, tool_handle, input_handle, websocket):
        nonlocal current_voice

        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        logger.info("Tool call: %s - %s", tool_name, args)

        # Handle voice switching
        if tool_name.startswith("switch_to_"):
            new_voice_name = tool_name[len("switch_to_"):].capitalize()
            for v in gradbot.flagship_voices():
                if v.name.lower() == tool_name[len("switch_to_"):]:
                    new_voice_name = v.name
                    break

            try:
                new_voice = gradbot.flagship_voice(new_voice_name)
                current_voice = new_voice_name

                new_config = gradbot.SessionConfig(
                    voice_id=new_voice.voice_id,
                    instructions=get_system_prompt(new_voice_name),
                    language=new_voice.language,
                    tools=tools,
                    **merge_overrides(_OVERRIDES,
                        flush_duration_s=FLUSH_FOR_S,
                        rewrite_rules=new_voice.language.rewrite_rules,
                    ),
                )
                await input_handle.send_config(new_config)

                await websocket.send_json(
                    {
                        "type": "voice_change",
                        "voice_name": new_voice_name,
                        "description": new_voice.description,
                    }
                )

                await tool_handle.send(
                    json.dumps(
                        {
                            "success": True,
                            "message": f"Voice switched to {new_voice_name}.",
                        }
                    )
                )
            except RuntimeError as e:
                await tool_handle.send_error(str(e))

        # Handle weather
        elif tool_name == "get_weather":
            city = args.get("city", "")
            country_code = args.get("country_code")

            result = await fetch_weather(city, country_code)

            await websocket.send_json(
                {"type": "weather_result", **result}
            )

            await tool_handle.send(json.dumps(result))

        # Handle news
        elif tool_name == "get_news":
            source = args.get("source", "bbc")
            count = min(args.get("count", 5), 10)

            result = await fetch_news(source, count)

            await websocket.send_json(
                {"type": "news_result", **result}
            )

            await tool_handle.send(json.dumps(result))

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
    app.mount(
        "/static",
        StaticFiles(directory=static_dir, follow_symlink=True),
        name="static",
    )


@app.get("/")
async def index():
    """Serve the main page."""
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
