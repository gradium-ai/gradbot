# Gradbot

A Rust-based voice AI framework for building real-time conversational agents with speech-to-text, LLM processing, and text-to-speech.

## Structure

```
gradbot/
├── gradbot_lib/         # Core library (STT/LLM/TTS multiplexing)
├── pygradbot/           # Python bindings (PyO3 + maturin)
├── gradbot_server/      # Standalone WebSocket server (remote mode)
├── src/                 # Server binary (OpenAI & Twilio WebSocket protocols)
├── js_audio_processor/  # Browser audio worklet (Opus encode/decode, jitter buffer)
├── demos/               # Example applications (see below)
└── configs/             # Server configuration files
```

## Quick Start

The easiest way to get started is with one of the demos using the Python bindings:

```bash
cd demos/simple_chat
uv sync                  # builds pygradbot from source via maturin
```

Set your API keys:

```bash
export GRADIUM_API_KEY=your_gradium_key

# Point to a fast LLM that supports tool calls (e.g., GPT-4o-mini, Claude, Groq)
export LLM_API_KEY=your_llm_key
export LLM_BASE_URL=...  # any OpenAI-compatible endpoint (LM Studio, Ollama, etc.)
```

Run:

```bash
uv run uvicorn main:app --reload
```

Then open http://localhost:8000.

## Demos

Every demo is a standalone FastAPI + WebSocket app. Pick one, `uv sync`, and run it.

| Demo | What it does | Key concepts |
|------|-------------|-------------|
| **[simple_chat](demos/simple_chat/)** | Basic voice conversation with 14 voices across 5 languages | Minimal starting point, dynamic voice/prompt switching |
| **[fantasy_shop](demos/fantasy_shop/)** | Haggling game — buy a sword from NPCs with distinct personalities | Tool calling, multi-character, game state management |
| **[voice_changer](demos/voice_changer/)** | AI switches between voice personas autonomously | AI-driven tool calls for voice switching |
| **[egg_timer](demos/egg_timer/)** | Voice assistant that can set timers | Background async tasks, tool calling |
| **[spanish_teacher](demos/spanish_teacher/)** | Language lesson with pronunciation practice | Educational UX, hiding imperfect STT from the learner |
| **[web_search](demos/web_search/)** | Voice-powered search — ask a question, get results | Async/deferred tool calls (AI talks while search runs) |
| **[hotel](demos/hotel/)** | Hotel booking agent for Paris, Bali, Dubai | Deferred tool results with natural chit-chat during wait |
| **[news_weather](demos/news_weather/)** | Live weather and news headlines | Free API integration (Open-Meteo, RSS feeds) |
| **[business_bank](demos/business_bank/)** | Banking agent with PIN auth, lost cards, loans | Security flows, multi-step business logic |
| **[mtg_adviser](demos/mtg_adviser/)** | Magic: The Gathering deck building assistant | External API integration (Scryfall) |
| **[mcp_demo](demos/mcp_demo/)** | Voice AI connected to MCP servers | Plug in any MCP server for instant tool access |
| **[voice_text_adventure](demos/voice_text_adventure/)** | Play 50+ classic text adventures (Zork, etc.) by voice | Game engine integration, dramatic narration styles |

## Vibe-coding a new demo

The fastest way to build a new voice app is to copy an existing demo and modify it with an AI coding assistant. Every demo follows the same pattern:

### 1. Copy the template

```bash
cp -r demos/simple_chat demos/my_demo
cd demos/my_demo
uv sync
```

### 2. Describe what you want

Open the demo in your AI coding tool (Claude Code, Cursor, etc.) and describe your idea. A good prompt includes:

- **The persona** — who is the AI character?
- **The tools** — what actions can the AI take?
- **The game/app logic** — what state do you track?

For example:

> Make this a pizza ordering assistant. The AI is "Marco", a friendly Italian pizzaiolo.
> He can: check_menu, add_to_order, remove_from_order, confirm_order.
> Track the order items and total price. When the order is confirmed, show a summary.

### 3. What to change

A demo has three parts:

| File | What to edit | What it controls |
|------|-------------|-----------------|
| `main.py` | System prompt, tool definitions, tool handlers | The AI's personality and what it can do |
| `static/index.html` | UI layout, colors, game state display | What the user sees |
| `config.yaml` (optional) | TTS/STT/session settings | Voice tuning |

The core loop in every `main.py` looks like this:

```python
# 1. Define tools
tools = [
    pygradbot.ToolDef(
        name="add_to_order",
        description="Add a pizza to the order",
        parameters_json='{"type": "object", "properties": {"pizza": {"type": "string"}}, "required": ["pizza"]}'
    ),
]

# 2. Start session with a system prompt and tools
config = pygradbot.SessionConfig(
    voice_id=voice.voice_id,
    instructions="You are Marco, a friendly pizzaiolo...",
    language=pygradbot.Lang.En,
    tools=tools,
    assistant_speaks_first=True,
)
input_handle, output_handle = await pygradbot.run(
    session_config=config,
    input_format=pygradbot.AudioFormat.OggOpus,
    output_format=pygradbot.AudioFormat.OggOpus,
)

# 3. Handle tool calls in the output loop
if msg.msg_type == "tool_call":
    if msg.tool_call.tool_name == "add_to_order":
        args = json.loads(msg.tool_call.args_json)
        order.append(args["pizza"])
        await msg.tool_call_handle.send(json.dumps({"result": "Added!"}))
```

### 4. Tips

- **Start from `simple_chat`** for basic conversations, or **`fantasy_shop`** if you need tool calling and game state.
- **Don't overthink the frontend** — the AI assistant can update the HTML for you. Describe the UI you want.
- **Deferred tool calls** — if a tool takes time (API call, search), just delay the `tool_handle.send()`. The AI will keep talking naturally while waiting. See `hotel` and `web_search` for examples.
- **Voice selection** — use `pygradbot.flagship_voices()` to list all 14 voices, or `pygradbot.flagship_voice("emma")` to pick one by name.
- **Mid-conversation config changes** — call `input_handle.send_config(new_config)` to switch voice, language, or system prompt without restarting.

## Building from source

```bash
cargo build              # debug build
cargo build --release    # release build
cargo clippy             # lint
```

## Python bindings

See [pygradbot/README.md](pygradbot/README.md) for the full Python API reference.

## Architecture

**gradbot_lib** coordinates three services in a real-time multiplexing loop:

- **STT** (Speech-to-Text) — streams microphone audio to Gradium ASR
- **LLM** — sends transcriptions to an OpenAI-compatible API, handles tool calls
- **TTS** (Text-to-Speech) — streams LLM responses to Gradium TTS, encodes audio

The multiplexer handles interruptions (user speaks while AI is talking), turn tracking, and graceful audio fade-out.

**Transport layer** supports two WebSocket protocols:
- OpenAI Realtime API compatible (`ws-openai`)
- Twilio Media Streams (`twilio`)

**gradbot_server** is a standalone WebSocket server for hosted deployment. It runs the STT/LLM/TTS coordination loop remotely while Python clients connect over WS to stream audio and handle tool calls. See [Remote Mode](#remote-mode) below.

**Browser audio** (`js_audio_processor/`) provides an AudioWorklet-based pipeline with Opus encoding/decoding, jitter buffering, and synchronized text display.

## Remote Mode

For hosted deployment, `gradbot_server` runs the coordination loop on a server with its own LLM credentials, while clients connect over WebSocket. This lets you host the server centrally and only distribute a `GRADIUM_API_KEY` to clients for STT/TTS billing.

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
# Fields here override any client-provided values (e.g., lock down LLM config)
# llm_extra_config = '{"reasoning": {"effort": "none"}}'
```

### Connecting from Python

Demos automatically use remote mode when `gradbot_server` is configured in `config.yaml`:

```yaml
gradbot_server:
  url: "wss://your-server.com/ws"
  api_key: "grd_..."
```

No code changes needed — `pygradbot.run()` transparently proxies over the WebSocket. You can also connect explicitly:

```python
input_handle, output_handle = await pygradbot.run(
    gradbot_url="wss://your-server.com/ws",
    gradbot_api_key="grd_...",
    session_config=config,
    input_format=pygradbot.AudioFormat.OggOpus,
    output_format=pygradbot.AudioFormat.OggOpus,
)
# Same handles, same API — tool calls, events, everything works identically
```

### Config pinning

The server can pin config fields (e.g., LLM credentials) so clients can't override them. On connection, the server reports which fields were pinned (field names only, never values).
