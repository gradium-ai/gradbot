"""
MCP Voice Demo - AI assistant powered by MCP servers

A FastAPI backend that exposes:
- GET /api/voices - list available flagship voices
- GET /api/tools - list discovered MCP tools
- WebSocket /ws/chat - real-time voice conversation with MCP tools

On each connection, the demo:
1. Spawns MCP servers as subprocesses (filesystem + memory by default)
2. Discovers available tools via MCP protocol
3. Bridges tool calls between the voice AI and MCP servers

Run with: uvicorn main:app --reload
"""

import asyncio
import os
import json
import sys
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pygradbot
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Initialize Rust logging (outputs to stderr)
pygradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)

# Default MCP server configurations
DEFAULT_MCP_SERVERS = [
    {
        "name": "filesystem",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/mcp-workspace"],
    },
    {
        "name": "memory",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
    },
]

# Load MCP server config from YAML if present, otherwise use defaults
_MCP_SERVERS_CFG = _YAML_CFG.get("mcp", {}).get("servers", DEFAULT_MCP_SERVERS)


class MCPServer:
    """Manages a single MCP server connection."""

    def __init__(self, name: str, command: str, args: list[str]):
        self.name = name
        self.command = command
        self.args = args
        self.session: ClientSession | None = None
        self.tools: list = []
        self.tool_names: set[str] = set()
        self._stack: AsyncExitStack | None = None

    async def connect(self):
        """Start the MCP server subprocess and connect."""
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=None,
        )
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

        # Discover tools
        tools_response = await self.session.list_tools()
        self.tools = tools_response.tools
        self.tool_names = {t.name for t in self.tools}

        print(f"MCP [{self.name}] connected — {len(self.tools)} tools: {', '.join(self.tool_names)}")

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this MCP server."""
        if not self.session:
            raise RuntimeError(f"MCP server '{self.name}' not connected")

        result = await self.session.call_tool(tool_name, arguments=arguments)

        # Extract text content from result
        parts = []
        if hasattr(result, "content") and result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
        return "\n".join(parts) if parts else str(result)

    async def disconnect(self):
        """Shut down the MCP server."""
        try:
            if self._stack:
                await self._stack.aclose()
        except Exception as e:
            print(f"MCP [{self.name}] close error: {e}")
        self.session = None
        print(f"MCP [{self.name}] disconnected")


class MCPManager:
    """Manages multiple MCP servers and routes tool calls."""

    def __init__(self, server_configs: list[dict]):
        self.server_configs = server_configs
        self.servers: list[MCPServer] = []
        self._tool_to_server: dict[str, MCPServer] = {}

    async def connect_all(self):
        """Connect to all configured MCP servers."""
        for cfg in self.server_configs:
            server = MCPServer(
                name=cfg["name"],
                command=cfg["command"],
                args=cfg["args"],
            )
            try:
                await server.connect()
                self.servers.append(server)
                for tool_name in server.tool_names:
                    self._tool_to_server[tool_name] = server
            except Exception as e:
                print(f"MCP [{cfg['name']}] failed to connect: {e}")
                import traceback
                traceback.print_exc()

    async def disconnect_all(self):
        """Disconnect all MCP servers."""
        for server in self.servers:
            await server.disconnect()
        self.servers.clear()
        self._tool_to_server.clear()

    def all_tools(self) -> list:
        """Get all tools from all connected servers."""
        tools = []
        for server in self.servers:
            tools.extend(server.tools)
        return tools

    def to_gradbot_tools(self) -> list[pygradbot.ToolDef]:
        """Convert all MCP tools to pygradbot ToolDef objects."""
        gradbot_tools = []
        for tool in self.all_tools():
            schema = getattr(tool, "inputSchema", {}) or {}
            gradbot_tools.append(
                pygradbot.ToolDef(
                    name=tool.name,
                    description=tool.description or tool.name,
                    parameters_json=json.dumps(schema),
                )
            )
        return gradbot_tools

    def tool_descriptions(self) -> list[dict]:
        """Get tool info for the API/UI."""
        result = []
        for server in self.servers:
            for tool in server.tools:
                result.append({
                    "name": tool.name,
                    "description": tool.description or tool.name,
                    "server": server.name,
                })
        return result

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Route a tool call to the appropriate MCP server."""
        server = self._tool_to_server.get(tool_name)
        if not server:
            raise ValueError(f"Unknown MCP tool: {tool_name}")
        return await server.call_tool(tool_name, arguments)


def lang_to_code(lang: pygradbot.Lang) -> str:
    """Convert Lang enum to language code."""
    if lang == pygradbot.Lang.En:
        return "en"
    elif lang == pygradbot.Lang.Fr:
        return "fr"
    elif lang == pygradbot.Lang.De:
        return "de"
    elif lang == pygradbot.Lang.Es:
        return "es"
    elif lang == pygradbot.Lang.Pt:
        return "pt"
    return "en"


def build_voice_tools() -> list[pygradbot.ToolDef]:
    """Build tool definitions for each voice."""
    tools = []
    for voice in pygradbot.flagship_voices():
        tool = pygradbot.ToolDef(
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


def get_system_prompt(current_voice_name: str, tool_descriptions: list[dict]) -> str:
    """Build the system prompt with discovered MCP tools."""
    voice = pygradbot.flagship_voice(current_voice_name)

    tools_by_server: dict[str, list[dict]] = {}
    for td in tool_descriptions:
        tools_by_server.setdefault(td["server"], []).append(td)

    tools_section = ""
    for server_name, tools in tools_by_server.items():
        tools_section += f"\n{server_name.upper()} tools:\n"
        for t in tools:
            tools_section += f"- {t['name']}: {t['description']}\n"

    return f"""You are {voice.name}, a friendly AI assistant with powerful capabilities provided by connected tools.

{voice.description}

YOUR CAPABILITIES:
{tools_section}
You can also switch between different voice personas using switch_to_* tools.

HOW TO USE TOOLS:
- When the user asks you to do something that matches a tool's capability, use it
- For file operations, always tell the user what you're about to do and what happened
- For memory operations, confirm what you stored or retrieved
- If a tool call fails, explain the error in plain language

CONVERSATION STYLE:
- Keep responses conversational and natural — you're a voice assistant
- Don't read out raw JSON or technical details — interpret results for the user
- Be proactive: if you created a file, mention what's in it; if you read one, summarize it
- For the memory/knowledge graph tools, be conversational about what you remember

Start by greeting the user and briefly mentioning what you can help with!
"""


# Tool descriptions populated by the first WebSocket connection
_app_tool_descriptions: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting MCP Voice Demo...")
    # Ensure workspace directory exists
    os.makedirs("/tmp/mcp-workspace", exist_ok=True)
    yield
    print("Shutting down...")


app = FastAPI(title="MCP Voice Demo", lifespan=lifespan)


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
        for v in pygradbot.flagship_voices()
    ]
    return JSONResponse(content={"voices": voices})


@app.get("/api/tools")
async def list_tools():
    """Return discovered MCP tools."""
    return JSONResponse(content={"tools": _app_tool_descriptions})


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for voice chat with MCP tools.

    Protocol:
    - Client sends JSON: {"type": "start", "voice_name": "Emma"}
    - Client sends binary: audio data (Ogg Opus)
    - Server sends JSON: {"type": "transcript", "text": "...", "is_user": true/false}
    - Server sends JSON: {"type": "voice_change", "voice_name": "..."}
    - Server sends JSON: {"type": "tool_result", "tool": "...", "result": "..."}
    - Server sends JSON: {"type": "event", "event": "..."}
    - Server sends binary: audio data
    - Client sends JSON: {"type": "stop"} to end
    """
    await websocket.accept()

    mcp_mgr = MCPManager(_MCP_SERVERS_CFG)

    try:
        # Wait for start message
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        voice_name = start_msg.get("voice_name", "Emma")

        # Validate voice
        try:
            voice = pygradbot.flagship_voice(voice_name)
        except RuntimeError:
            await websocket.close(
                code=4001, reason=f"Unknown voice: {voice_name}"
            )
            return

        current_voice = voice_name
        print(f"Starting MCP chat with voice={voice_name}")

        # Connect MCP servers
        await websocket.send_json({"type": "status", "message": "Connecting to MCP servers..."})
        await mcp_mgr.connect_all()

        if not mcp_mgr.servers:
            await websocket.send_json({"type": "error", "message": "No MCP servers connected"})
            await websocket.close(code=4002, reason="No MCP servers")
            return

        tool_descs = mcp_mgr.tool_descriptions()
        # Cache for the /api/tools endpoint
        global _app_tool_descriptions
        _app_tool_descriptions = tool_descs
        await websocket.send_json({"type": "tools_discovered", "tools": tool_descs})

        # Build tools
        voice_tools = build_voice_tools()
        mcp_tools = mcp_mgr.to_gradbot_tools()
        tools = voice_tools + mcp_tools
        print(f"Built {len(tools)} tools: {len(voice_tools)} voice + {len(mcp_tools)} MCP")

        # Create session config
        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_system_prompt(voice_name, tool_descs),
            language=voice.language,
            tools=tools,
            **merge_overrides(
                _OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=voice.language.rewrite_rules,
            ),
        )

        # Start session
        input_handle, output_handle = await pygradbot.run(
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.Pcm
            if USE_PCM
            else pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_tool_call(tool_call, tool_handle):
            """Handle MCP and voice switching tool calls."""
            nonlocal current_voice, tools

            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)

            print(f"Tool call: {tool_name} - {args}")

            # Handle voice switching
            if tool_name.startswith("switch_to_"):
                new_voice_name = tool_name[len("switch_to_"):].capitalize()
                for v in pygradbot.flagship_voices():
                    if v.name.lower() == tool_name[len("switch_to_"):]:
                        new_voice_name = v.name
                        break

                try:
                    new_voice = pygradbot.flagship_voice(new_voice_name)
                    current_voice = new_voice_name

                    new_config = pygradbot.SessionConfig(
                        voice_id=new_voice.voice_id,
                        instructions=get_system_prompt(new_voice_name, tool_descs),
                        language=new_voice.language,
                        tools=tools,
                        **merge_overrides(
                            _OVERRIDES,
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

            # Handle MCP tools
            elif mcp_mgr.has_tool(tool_name):
                try:
                    result_text = await mcp_mgr.call_tool(tool_name, args)

                    # Notify client
                    await websocket.send_json(
                        {
                            "type": "tool_result",
                            "tool": tool_name,
                            "result": result_text[:500],  # Truncate for UI
                        }
                    )

                    await tool_handle.send(
                        json.dumps({"success": True, "result": result_text})
                    )
                except Exception as e:
                    print(f"MCP tool error: {e}")
                    await tool_handle.send_error(str(e))

            else:
                await tool_handle.send_error(f"Unknown tool: {tool_name}")

        async def process_output():
            """Receive output from gradbot and send to client."""
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        break

                    if msg.msg_type == "audio":
                        await websocket.send_json(
                            {
                                "type": "audio_timing",
                                "start_s": msg.start_s,
                                "stop_s": msg.stop_s,
                                "turn_idx": msg.turn_idx,
                                "interrupted": msg.interrupted,
                            }
                        )
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "text": msg.text,
                                "is_user": False,
                                "stop_s": msg.stop_s,
                                "turn_idx": msg.turn_idx,
                            }
                        )

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "text": msg.text,
                                "is_user": True,
                            }
                        )

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(
                            handle_tool_call(
                                msg.tool_call, msg.tool_call_handle
                            )
                        )

                    elif msg.msg_type == "event":
                        await websocket.send_json(
                            {
                                "type": "event",
                                "event": msg.event.event_type,
                            }
                        )

                except Exception as e:
                    print(f"Output processing error: {e}")
                    try:
                        await websocket.send_json(
                            {"type": "error", "message": str(e)}
                        )
                    except:
                        pass
                    break

        async def receive_audio():
            """Receive audio from client and send to gradbot."""
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        msg_type = data.get("type")
                        if msg_type == "stop":
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

        # Run both tasks
        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await mcp_mgr.disconnect_all()
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
