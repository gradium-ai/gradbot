# gradbot

Python bindings for the gradbot voice AI library. Real-time speech-to-speech with tool calling.

## Installation

```bash
pip install gradbot
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GRADIUM_API_KEY` | Yes | API key for Gradium STT/TTS services |
| `LLM_API_KEY` | Yes | API key for OpenAI-compatible LLM |
| `LLM_BASE_URL` | No | LLM API base URL (defaults to OpenAI) |
| `LLM_MODEL` | No | LLM model name (auto-detected if only one available) |
| `GRADIUM_BASE_URL` | No | Base URL for Gradium services |

## Quick Start

```python
import asyncio
import gradbot

async def main():
    input_handle, output_handle = await gradbot.run(
        session_config=gradbot.SessionConfig(
            voice_id="YTpq7expH9539ERJ",
            instructions="You are a helpful assistant.",
            language=gradbot.Lang.En,
        ),
        input_format=gradbot.AudioFormat.OggOpus,
        output_format=gradbot.AudioFormat.OggOpus,
    )

    while True:
        msg = await output_handle.receive()
        if msg is None:
            break
        if msg.msg_type == "audio":
            play(msg.data)  # bytes
        elif msg.msg_type == "tool_call":
            result = handle(msg.tool_call.tool_name, msg.tool_call.args_json)
            await msg.tool_call_handle.send(result)

asyncio.run(main())
```

### Remote Mode

Connect to a `gradbot_server` instead of running STT/LLM/TTS locally:

```python
input_handle, output_handle = await gradbot.run(
    gradbot_url="wss://your-server.com/ws",
    gradbot_api_key="grd_...",
    session_config=config,
    input_format=gradbot.AudioFormat.OggOpus,
    output_format=gradbot.AudioFormat.OggOpus,
)
```

When `gradbot_url` is set, all other client params are ignored. The server handles everything.

## FastAPI Integration

The `gradbot.websocket` and `gradbot.routes` modules provide a WebSocket handler and route setup for building voice demos.

```python
import fastapi
import gradbot

app = fastapi.FastAPI()
cfg = gradbot.config.from_env()

gradbot.routes.setup(app, config=cfg, static_dir="static", with_voices=True)

@app.websocket("/ws")
async def ws(websocket: fastapi.WebSocket):
    await gradbot.websocket.handle_session(
        websocket,
        config=cfg,
        on_start=lambda msg: gradbot.SessionConfig(
            instructions="You are a helpful assistant.",
        ),
    )
```

`gradbot.routes.setup` registers `/api/audio-config`, serves your static files, and automatically serves the bundled JS audio processor at `/static/js/`.

### WebSocket Protocol

| Direction | Format | Description |
|---|---|---|
| Client → Server | JSON `{"type": "start", ...}` | Begin session |
| Client → Server | Binary | Audio data |
| Client → Server | JSON `{"type": "config", ...}` | Reconfigure mid-session |
| Client → Server | JSON `{"type": "stop"}` | End session |
| Server → Client | JSON | Transcripts, events, audio timing |
| Server → Client | Binary | Audio data |

## API Reference

### Functions

- **`run(...)`:** Create clients and start a session. Returns `(SessionInputHandle, SessionOutputHandle)`.
- **`create_clients(...)`:** Create reusable `GradbotClients` for multiple sessions.
- **`init_logging()`:** Initialize debug logging.

### Enums

| Enum | Values |
|---|---|
| `Lang` | `En`, `Fr`, `Es`, `De`, `Pt` |
| `Gender` | `Masculine`, `Feminine` |
| `Country` | `Us`, `Gb`, `Fr`, `De`, `Mx`, `Es`, `Br` |
| `AudioFormat` | `OggOpus`, `Pcm`, `Ulaw` |

### Classes

- **`SessionConfig`:** `voice_id`, `instructions`, `language`, `assistant_speaks_first`, `silence_timeout_s`, `tools`
- **`ToolDef`:** `name`, `description`, `parameters_json`
- **`SessionInputHandle`:** `send_audio(bytes)`, `send_config(SessionConfig)`, `close()`
- **`SessionOutputHandle`:** `receive() -> MsgOut | None`
- **`MsgOut`:** `msg_type` is one of `"audio"`, `"tts_text"`, `"stt_text"`, `"event"`, `"tool_call"`
- **`ToolCallInfo`:** `call_id`, `tool_name`, `args_json`
- **`ToolCallHandlePy`:** `send(result_json)`, `send_error(error_message)`

## Examples

See [`demos/`](../demos/) for complete examples including tool calling, voice switching, and WebSocket frontends.
