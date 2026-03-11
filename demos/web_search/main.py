"""
Web Search Demo - Voice-powered web search assistant

A voice agent that searches the web using DuckDuckGo when the user asks questions.
Results appear in a side panel while the AI discusses and summarizes findings.

Run with: uvicorn main:app --reload
"""

import asyncio
import os
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from ddgs import DDGS

import gradbot

gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)


AGENT_VOICES = {
    "Alex": "Eva",
    "Nova": "Sydney",
}


def get_prompt(agent_name: str) -> str:
    return f"""You are {agent_name}, a knowledgeable and curious research assistant.
You help users find information about anything by searching the web.

YOUR PERSONALITY:
- Curious, sharp, and enthusiastic about learning
- You love diving into topics and finding interesting details
- You explain things clearly and concisely
- You're honest about what you know and don't know

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* - just speak naturally
- Be conversational and natural

SEARCH BEHAVIOR — YOUR #1 RULE:
- You MUST call web_search for EVERY user question, request, or topic. No exceptions.
- Do NOT answer from memory. Do NOT say "I think" or "I believe". ALWAYS search first.
- Call web_search FIRST, THEN talk. Never talk without searching.
- The INSTANT the user mentions any topic, person, event, place, or question — call web_search.
- After calling the tool, share a brief thought while the search runs.
- When results arrive, discuss the most relevant findings.
- If results don't fully answer the question, suggest refining the search.

Saying "I don't need to search for that" or answering without calling web_search is the WORST mistake you can make.

NEVER FABRICATE DATA:
- NEVER make up search results or pretend you have results before they arrive
- ONLY discuss information that came back from a tool call
- If the tool result hasn't arrived yet, say you're still looking

Start by greeting the user and asking what they'd like to search for.
"""


def build_tools() -> list[gradbot.ToolDef]:
    return [
        gradbot.ToolDef(
            name="web_search",
            description="Search the web for information. Use this whenever the user asks about anything. Keep chatting while waiting for results!",
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
    ]


async def do_search(query: str, retries: int = 3) -> list[dict]:
    """Run DuckDuckGo search in a thread pool with retries for rate limiting."""
    loop = asyncio.get_event_loop()
    for attempt in range(retries):
        try:
            results = await loop.run_in_executor(
                None, lambda: DDGS().text(query, max_results=5)
            )
            if results:
                print(f"Search '{query}': {len(results)} results (attempt {attempt + 1})")
                return results
            print(f"Search '{query}': empty results (attempt {attempt + 1}/{retries})")
        except Exception as e:
            print(f"Search '{query}': error on attempt {attempt + 1}/{retries}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(1.5 * (attempt + 1))
    return []  # Each: {"title": str, "href": str, "body": str}


@dataclass
class SessionState:
    pending_tasks: list[asyncio.Task] = field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Web Search Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Web Search Demo", lifespan=lifespan)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    state = SessionState()

    try:
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        agent_name = start_msg.get("agent", "Alex")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        voice_key = AGENT_VOICES.get(agent_name, "Eva")
        print(f"Starting web search chat with {agent_name} (voice: {voice_key}, padding_bonus: {padding_bonus})")

        voice = gradbot.flagship_voice(voice_key)
        tools = build_tools()

        config = gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_prompt(agent_name),
            language=gradbot.Lang.En,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="en",
                assistant_speaks_first=True,
            ),
        )

        input_handle, output_handle = await gradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=gradbot.AudioFormat.OggOpus,
            output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_web_search(query: str, tool_handle):
            """Search the web and send results to frontend + LLM."""
            # Notify frontend immediately
            await websocket.send_json({
                "type": "tool_started",
                "message": f"Searching for \"{query}\"...",
            })

            try:
                results = await do_search(query)
            except Exception as e:
                print(f"Search error: {e}")
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

            # Send results to frontend
            await websocket.send_json({
                "type": "search_results",
                "query": query,
                "results": results,
            })

            # Send results to LLM
            await tool_handle.send(json.dumps({
                "success": True,
                "query": query,
                "results": results,
                "message": f"Found {len(results)} results for \"{query}\". Discuss the most relevant findings with the user. Be concise.",
            }))

            print(f"Search complete for '{query}': {len(results)} results")

        async def handle_tool_call(tool_call, tool_handle):
            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)
            print(f"Tool call: {tool_name} - {args}")

            if tool_name == "web_search":
                query = args.get("query", "")
                task = asyncio.create_task(handle_web_search(query, tool_handle))
                state.pending_tasks.append(task)
                task.add_done_callback(lambda t: state.pending_tasks.remove(t) if t in state.pending_tasks else None)
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
