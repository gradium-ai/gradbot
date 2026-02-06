# Gradbot

A Rust-based voice AI framework for building real-time conversational agents with speech-to-text, LLM processing, and text-to-speech.

## Structure

```
gradbot/
├── gradbot_lib/         # Core library (STT/LLM/TTS multiplexing)
├── pygradbot/           # Python bindings (PyO3 + maturin)
├── src/                 # Server binary (OpenAI & Twilio WebSocket protocols)
├── js_audio_processor/  # Browser audio worklet (Opus encode/decode, jitter buffer)
├── demos/               # Example applications
│   ├── simple_chat/     # Basic voice chat
│   ├── voice_changer/   # Multi-voice switching (14 voices, 5 languages)
│   ├── fantasy_shop/    # Haggling game with tool calling
│   ├── egg_timer/       # Voice assistant with timer tool
│   ├── spanish_teacher/ # Language learning demo
│   └── voice_text_adventure/  # Play classic text adventures by voice
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
export OPENAI_API_KEY=your_llm_key
export LLM_BASE_URL=...  # any OpenAI-compatible endpoint
```

Run:

```bash
uv run uvicorn main:app --reload
```

Then open http://localhost:8000.

## Building from source

```bash
cargo build              # debug build
cargo build --release    # release build
cargo clippy             # lint
```

## Python bindings

See [pygradbot/README.md](pygradbot/README.md) for the Python API reference.

## Architecture

**gradbot_lib** coordinates three services in a real-time multiplexing loop:

- **STT** (Speech-to-Text) — streams microphone audio to Gradium ASR
- **LLM** — sends transcriptions to an OpenAI-compatible API, handles tool calls
- **TTS** (Text-to-Speech) — streams LLM responses to Gradium TTS, encodes audio

The multiplexer handles interruptions (user speaks while AI is talking), turn tracking, and graceful audio fade-out.

**Transport layer** supports two WebSocket protocols:
- OpenAI Realtime API compatible (`ws-openai`)
- Twilio Media Streams (`twilio`)

**Browser audio** (`js_audio_processor/`) provides an AudioWorklet-based pipeline with Opus encoding/decoding, jitter buffering, and synchronized text display.
