# gradbot

Python bindings for the gradbot voice AI library.

## Installation

Build and install in development mode:

```bash
cd gradbot_py
maturin develop
```

Or build a wheel:

```bash
maturin build --release
pip install target/wheels/gradbot-*.whl
```

## Quick Start

```python
import asyncio
import gradbot

# Initialize logging (optional)
gradbot.init_logging()

async def main():
    # Create session with default settings
    input_handle, output_handle = await gradbot.run(
        session_config=gradbot.SessionConfig(
            voice_id="YTpq7expH9539ERJ",  # Emma voice
            instructions="You are a helpful assistant.",
            language=gradbot.Lang.En,
        ),
        input_format=gradbot.AudioFormat.OggOpus,
        output_format=gradbot.AudioFormat.OggOpus,
    )

    # Send audio and receive responses
    # ... your audio handling code ...

asyncio.run(main())
```

## Environment Variables

- `GRADIUM_API_KEY` - API key for Gradium STT/TTS services (required)
- `GRADIUM_BASE_URL` - Base URL for Gradium services (optional)
- `LLM_API_KEY` - API key for OpenAI-compatible LLM API (required)
- `LLM_BASE_URL` - Base URL for LLM API (optional, defaults to OpenAI)
- `LLM_MODEL` - LLM model name (optional, auto-detected if single model available)

## API Reference

### Functions

#### `init_logging()`
Initialize tracing subscriber for debug logging. Call once at startup.

#### `flagship_voices() -> list[FlagshipVoice]`
Returns all available flagship voices.

```python
for voice in gradbot.flagship_voices():
    print(f"{voice.name}: {voice.voice_id} ({voice.language})")
```

#### `flagship_voice(name: str) -> FlagshipVoice`
Look up a flagship voice by name (case-insensitive).

```python
voice = gradbot.flagship_voice("emma")
print(voice.voice_id)  # "YTpq7expH9539ERJ"
```

#### `create_clients(...) -> GradbotClients`
Create reusable clients for multiple sessions.

```python
clients = await gradbot.create_clients(
    gradium_api_key="...",  # or use GRADIUM_API_KEY env var
    llm_base_url="https://api.openai.com/v1",
)
```

#### `run(...) -> tuple[SessionInputHandle, SessionOutputHandle]`
Create clients and start a session in one call.

```python
input_handle, output_handle = await gradbot.run(
    session_config=config,
    input_format=gradbot.AudioFormat.OggOpus,
    output_format=gradbot.AudioFormat.OggOpus,
)
```

**Remote mode** — connect to a `gradbot_server` instead of running STT/LLM/TTS locally:

```python
input_handle, output_handle = await gradbot.run(
    gradbot_url="wss://your-server.com/ws",
    gradbot_api_key="grd_...",
    session_config=config,
    input_format=gradbot.AudioFormat.OggOpus,
    output_format=gradbot.AudioFormat.OggOpus,
)
```

When `gradbot_url` is set, all other client params (`gradium_api_key`, `llm_*`, etc.) are ignored — the server handles STT/LLM/TTS. The returned handles behave identically to local mode.

### Classes

#### `Lang`
Language enum: `En`, `Fr`, `Es`, `De`, `Pt`

#### `Gender`
Voice gender: `Masculine`, `Feminine`

#### `Country`
Voice country/accent: `Us`, `Gb`, `Fr`, `De`, `Mx`, `Es`, `Br`

#### `AudioFormat`
Audio encoding format:
- `OggOpus` - Ogg container with Opus codec
- `Pcm` - Raw PCM (24kHz input, 48kHz output)
- `Ulaw` - G.711 mu-law (for telephony)

#### `SessionConfig`
Session configuration:

```python
config = gradbot.SessionConfig(
    voice_id="YTpq7expH9539ERJ",      # Voice ID or None for default
    instructions="Be helpful.",        # System prompt
    language=gradbot.Lang.En,       # Language
    assistant_speaks_first=True,       # Start with greeting
    silence_timeout_s=5.0,             # Silence before prompting
    tools=[...],                       # Tool definitions for LLM
)
```

#### `ToolDef`
Tool definition for LLM function calling:

```python
tool = gradbot.ToolDef(
    name="get_weather",
    description="Get current weather for a location",
    parameters_json='{"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}'
)
```

#### `SessionInputHandle`
Handle for sending input to a session:

- `await send_audio(data: bytes)` - Send encoded audio
- `await send_config(config: SessionConfig)` - Update configuration
- `await close()` - Close the input handle

#### `SessionOutputHandle`
Handle for receiving output from a session:

- `await receive() -> MsgOut | None` - Get next message (None when session ends)

#### `MsgOut`
Output message with type-specific fields:

```python
msg = await output_handle.receive()
if msg is None:
    print("Session ended")
elif msg.msg_type == "audio":
    # msg.data: bytes, msg.start_s: float, msg.stop_s: float
    send_to_speaker(msg.data)
elif msg.msg_type == "tts_text":
    # msg.text: str, msg.start_s: float, msg.stop_s: float
    display_caption(msg.text)
elif msg.msg_type == "stt_text":
    # msg.text: str, msg.start_s: float
    display_transcription(msg.text)
elif msg.msg_type == "event":
    # msg.event: Event, msg.time_s: float
    handle_event(msg.event)
elif msg.msg_type == "tool_call":
    # msg.tool_call: ToolCallInfo, msg.tool_call_handle: ToolCallHandle
    result = await process_tool(msg.tool_call)
    await msg.tool_call_handle.send(json.dumps(result))
```

#### `ToolCallInfo`
Tool call information:
- `call_id: str` - Unique call ID
- `tool_name: str` - Name of the tool
- `args_json: str` - JSON string of arguments

#### `ToolCallHandlePy`
Handle for responding to tool calls:
- `await send(result_json: str)` - Send success result
- `await send_error(error_message: str)` - Send error result

## Example: Voice Chat with Tools

See `demos/fantasy_shop/main.py` for a complete example using FastAPI, WebSockets, and tool calling.

```python
import asyncio
import json
import gradbot

async def handle_session(websocket):
    # Define tools
    tools = [
        gradbot.ToolDef(
            name="get_time",
            description="Get the current time",
            parameters_json='{"type": "object", "properties": {}, "required": []}'
        )
    ]

    # Start session
    config = gradbot.SessionConfig(
        instructions="You are a helpful assistant with access to tools.",
        tools=tools,
    )
    input_handle, output_handle = await gradbot.run(
        session_config=config,
        input_format=gradbot.AudioFormat.OggOpus,
        output_format=gradbot.AudioFormat.OggOpus,
    )

    # Process messages
    async def process_output():
        while True:
            msg = await output_handle.receive()
            if msg is None:
                break
            if msg.msg_type == "audio":
                await websocket.send_bytes(msg.data)
            elif msg.msg_type == "tool_call":
                if msg.tool_call.tool_name == "get_time":
                    import datetime
                    result = {"time": datetime.datetime.now().isoformat()}
                    await msg.tool_call_handle.send(json.dumps(result))

    async def receive_audio():
        async for data in websocket.iter_bytes():
            await input_handle.send_audio(data)

    await asyncio.gather(process_output(), receive_audio())
```
