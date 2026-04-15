<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
    <img src="assets/logo-light.svg" alt="Gradbot" width="300" />
  </picture>
</p>

<p align="center">
  <strong>Open-source voice agent framework.<br>~50 lines of code. Any OpenAI-compatible LLM.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/gradbot/"><img src="https://img.shields.io/pypi/v/gradbot.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/gradbot/"><img src="https://img.shields.io/pypi/pyversions/gradbot.svg" alt="Python"></a>
  <a href="https://github.com/gradium-ai/gradbot/blob/main/LICENSE-MIT"><img src="https://img.shields.io/badge/license-MIT%2FApache--2.0-blue" alt="License"></a>
</p>

---

Gradbot gives you the event loop for voice agents. You write the logic, it handles the rest.

At its core is a multiplexing engine written in Rust that coordinates three streams in real time: **speech-to-text**, **LLM inference**, and **text-to-speech** while managing conversational state, turn-taking, and interruptions. It works with any **OpenAI-compatible LLM** (GPT-4o, Claude, Groq, Ollama, LM Studio, etc.) and uses **[Gradium](https://gradium.ai)** for streaming STT/TTS across multiple voices from the Gradium voice catalog and 5 languages.

Whether you're building a [haggling game](demos/fantasy_shop/) or a [travel booking assistant](demos/hotel/), Gradbot lets you go from idea to working voice experience in under 50 lines of code.

https://github.com/user-attachments/assets/290fbdb6-386c-4a2a-8c37-f30c9b023aec

## Features

- **STT, LLM, and TTS coordinated in one loop**: Rust multiplexer streams all three concurrently
- **Turn-taking, fillers, and barge-in handled automatically**: graceful audio fade-out on interruption
- **Bi-directional audio streaming**: with VAD and silence detection out of the box
- **Async tool calling**: the AI keeps talking naturally while slow tools execute in the background; lost calls are tracked and recovered
- **Live transcription and tool calls in the same cycle**: define tools as JSON Schema, handle results sync or async
- **Full Gradium voice library, 5 languages**: English, French, German, Spanish, Portuguese; unlimited voices via cloning
- **Mid-session reconfiguration**: change voice, language, prompt, or tools without restarting
- **MCP integration**: connect any MCP server for instant tool access
- **Remote mode**: deploy `gradbot_server` centrally; clients connect over WebSocket with the same API
- **Config pinning**: lock down LLM credentials server-side while clients control voice and prompt

## Quick Start

Install from PyPI:

```bash
pip install gradbot
```

Or run a demo directly (builds from source):

```bash
cd demos/simple_chat
uv sync
```

Set your API keys:

```bash
export GRADIUM_API_KEY=your_gradium_key

# Any OpenAI-compatible endpoint (OpenAI, Groq, Ollama, LM Studio, etc.)
export LLM_API_KEY=your_llm_key
export LLM_BASE_URL=...  # optional, defaults to OpenAI
```

Run:

```bash
uv run uvicorn main:app --reload
# Open http://localhost:8000
```

## Architecture

The multiplexer runs a state machine (**Listening** → **Flushing** → **Processing**) that handles concurrent audio streams, interruption detection, and turn management. Gradbot flushes trailing audio by pushing silence into the STT buffer so the LLM gets your complete utterance before replying. There is no extra latency from voice activity detection delays. If the user goes quiet, the LLM is prompted to re-engage naturally instead of creating dead air.

## Who Should Use Gradbot

Gradbot is built for **prototyping and experimentation**. Use it to hack on ideas and build voice experiences without spending hours on infrastructure.

- Support agents and real-time assistants
- Coaching and educational apps
- Tool-using voice workflows
- Games and experimental interfaces

Build weird, fun stuff. Voice agents don't have to be boring.

> **For production**, use Gradium's models through orchestrators like [LiveKit](https://docs.livekit.io/agents/models/tts/gradium/) and [Pipecat](https://reference-server.pipecat.ai/en/latest/api/pipecat.services.gradium.html) for enterprise-grade reliability and scaling.

## Demos

Every demo is a standalone FastAPI + WebSocket app. Pick one, `uv sync`, and run it.

| Demo | What it does | Key concepts |
|------|-------------|-------------|
| **[simple_chat](demos/simple_chat/)** | Basic voice conversation | Minimal starting point, dynamic voice/prompt switching |
| **[fantasy_shop](demos/fantasy_shop/)** | Haggling game: buy a sword from NPCs | Tool calling, multi-character, game state, deferred tools |
| **[hotel](demos/hotel/)** | Hotel booking agent for Paris, Bali, Dubai | Deferred tool results with natural chit-chat using Web Search API |
| **[business_bank](demos/business_bank/)** | Banking agent with PIN auth and loan applications | Security flows, multi-step business logic |
| **[restaurant_ordering](demos/restaurant_ordering/)** | Multilingual voice ordering agent for a fast-food restaurant | Menu browsing, order customization, multilingual support |
| **[npc_3d_game](demos/npc_3d_game/)** | 3D office exploration: solve voice riddles, handle NPC check-ins | Three.js, multi-session (clue + check-in), response classification |

## Building a New Demo

Copy an existing demo and modify it yourself, or ask your AI coding assistants:

```bash
cp -r demos/simple_chat demos/my_demo
cd demos/my_demo
uv sync
```

Every demo has three files:

| File | What to edit | What it controls |
|------|-------------|-----------------|
| `main.py` | System prompt, tool definitions, tool handlers | The AI's personality and capabilities |
| `static/index.html` | UI layout, colors, game state display | What the user sees |
| `config.yaml` | TTS/STT/session settings | Voice tuning (optional) |

There is a shared `demos/config.yaml` that all demos inherit from. Each demo can override it with its own `config.yaml`.

The core pattern in every `main.py`:

```python
# 1. Define tools
tools = [
    gradbot.ToolDef(
        "add_to_order",
        "Add a pizza to the order",
        '{"type": "object", "properties": {"pizza": {"type": "string"}}, "required": ["pizza"]}',
    ),
]

# 2. Configure the session via an on_start callback
def on_start(msg: dict) -> gradbot.SessionConfig:
    return gradbot.SessionConfig(
        voice_id="YTpq7expH9539ERJ",
        instructions="You are Marco, a friendly pizzaiolo...",
        language=gradbot.Lang.En,
        tools=tools,
        assistant_speaks_first=True,
    )

# 3. Handle tool calls
async def on_tool_call(handle, input_handle, websocket):
    if handle.name == "add_to_order":
        order.append(handle.args["pizza"])
        await handle.send_json({"result": "Added!"})

# 4. Wire it up
@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket):
    await gradbot.websocket.handle_session(
        websocket,
        config=gradbot.config.from_env(),
        on_start=on_start,
        on_tool_call=on_tool_call,
    )
```

### Tips

- **Start from `simple_chat`** for basic conversations, or **`fantasy_shop`** for tool calling and game state.
- **Deferred tool calls:** delay `tool_handle.send()` and the AI keeps talking while waiting. See `hotel` for an example.
- **Voice selection:** any voice from the Gradium voice library can be used by passing its `voice_id` to `SessionConfig`.
- **Mid-conversation changes:** `input_handle.send_config(new_config)` switches voice, language, or prompt without restarting.

## Integrations

| Category | Services |
|----------|----------|
| **STT** | [Gradium](https://gradium.ai) streaming ASR |
| **TTS** | [Gradium](https://gradium.ai) streaming TTS (full voice library, 5 languages) |
| **LLM** | Any OpenAI-compatible API: OpenAI, Groq, OpenRouter, Ollama, LM Studio, etc. |
| **Telephony** | Twilio Media Streams |
| **Tools** | MCP (Model Context Protocol), custom JSON Schema tools |
| **Transport** | WebSocket (OpenAI Realtime API compatible, Twilio protocol) |

## Remote Mode

For hosted deployment, `gradbot_server` runs the STT/LLM/TTS loop on a central server while clients connect over WebSocket. This keeps LLM credentials server-side.

### Running the server

```bash
cargo run -p gradbot_server -- --config server.toml
```

Example `server.toml`:

```toml
addr = "0.0.0.0"
port = 8080
gradium_base_url = "https://api.gradium.ai/api"

llm_base_url = "https://api.openai.com/v1"
llm_api_key = "$LLM_API_KEY"
llm_model_name = "gpt-4o"

[pinned]
# Fields listed here override client-provided values
# llm_extra_config = '{"reasoning": {"effort": "none"}}'
```

### Connecting from Python

Add to `config.yaml` (no code changes needed):

```yaml
gradbot_server:
  url: "wss://your-server.com/ws"
  api_key: "grd_..."
```

Or connect explicitly:

```python
input_handle, output_handle = await gradbot.run(
    gradbot_url="wss://your-server.com/ws",
    gradbot_api_key="grd_...",
    session_config=config,
    input_format=gradbot.AudioFormat.OggOpus,
    output_format=gradbot.AudioFormat.OggOpus,
)
# Same handles, same API - tool calls, events, everything works identically
```

## Configuration

Gradbot supports three layers of configuration, applied in order:

| Layer | Format | Used by |
|-------|--------|---------|
| Environment variables | `GRADIUM_API_KEY`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | All modes |
| YAML config | `demos/config.yaml` + per-demo overrides | Python demos |
| TOML config | `configs/gradbot.toml` | Rust server binary |

See [`demos/config.example.yaml`](demos/config.example.yaml) for all available YAML options and [`gradbot_server/config.example.toml`](gradbot_server/config.example.toml) for server configuration.

## Building from Source

### Rust

```bash
cargo build              # debug
cargo build --release    # release
cargo clippy             # lint
cargo test               # tests
```

### Python bindings

```bash
cd demos/simple_chat     # or any demo
uv sync                  # builds via maturin automatically
```

To rebuild after Rust changes:

```bash
uv sync --reinstall-package gradbot
```

Or use the Makefile:

```bash
make build DEMO=simple_chat   # build + install into one demo's venv
make build-all                # build + install into all demo venvs
make run DEMO=simple_chat     # run with uvicorn (auto-reload, excludes .venv)
```

### Docker

```bash
docker build -t gradbot .
docker run -e GRADIUM_API_KEY=grd_... -e LLM_API_KEY=sk-... -p 8000:8000 gradbot
```

## Project Structure

```
gradbot/
├── gradbot_lib/            # Core Rust library (STT/LLM/TTS multiplexing)
├── gradbot_py/             # Python bindings (PyO3 + maturin)
│   └── gradbot/            # Python package (fastapi helpers, config, audio worklet)
├── gradbot_server/         # Standalone WebSocket server (remote mode)
├── src/                    # Server binary (OpenAI & Twilio WebSocket protocols)
├── demos/                  # Example applications
│   ├── app.py              # Combined app mounting all demos (for Docker)
│   └── config.example.yaml # Configuration template
└── configs/                # TOML configs for the Rust server binary
```

## Python API Reference

See [gradbot_py/README.md](gradbot_py/README.md) for the full Python API documentation.

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
