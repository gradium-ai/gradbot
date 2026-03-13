# Gradbot Agent Guide

This document provides essential information for agents working with the Gradbot codebase.

## Project Overview

Gradbot is a Rust-based voice AI framework that provides real-time speech-to-text (STT), LLM processing, and text-to-speech (TTS) in a coordinated conversation loop.

**Important**: Gradbot is a **standalone workspace**, separate from the main compute-engine workspace. It has its own `Cargo.toml` with workspace dependencies and a patched `audiopus_sys` for Opus codec support.

### Workspace Structure

```
gradbot/
├── Cargo.toml           # Standalone workspace root
├── gradbot_lib/        # Core library (STT/LLM/TTS coordination)
├── gradbot_py/       # Python bindings via PyO3
├── src/                 # Server binary (OpenAI/Twilio protocols)
├── demos/               # Example applications
│   ├── simple_chat/     # Basic voice chat demo
│   ├── voice_changer/   # Multi-voice demo
│   └── fantasy_shop/    # Game with tool calling
└── js_audio_processor/  # Browser audio (shared via symlinks)
```

### Architecture

- **gradbot**: Self-contained core library with STT, LLM, TTS clients and multiplexing
- **Transport Layer**: Server supports OpenAI WebSocket (`ws-openai`) and Twilio protocols
- **Multiplex Module**: Coordinates STT, LLM, and TTS with interruption handling
- **Audio Processing**: 24kHz input, 48kHz output, using OGG Opus encoding
- **State Machine**: Handles Listening → Flushing → Processing transitions

## Essential Commands

### Building
```bash
# Build from gradbot directory (standalone workspace)
cd gradbot
cargo build

# Build release
cargo build --release

# Build specific crate
cargo build -p gradbot
cargo build -p gradbot_py
```

### Running Demos
```bash
# Simple chat demo (Python + FastAPI)
cd demos/simple_chat
uv sync
uv run uvicorn main:app --reload

# Fantasy shop game
cd demos/fantasy_shop
uv sync
uv run uvicorn main:app --reload
```

> **After changing gradbot_py Rust code**, you must reinstall the package in each demo venv:
> ```bash
> uv sync --reinstall-package gradbot
> ```
> A plain `uv sync` will not rebuild gradbot if the version number hasn't changed.

### Running Server Binary
```bash
# Run with config (configs are in parent compute-engine/configs/)
cargo run --release -- --config ../configs/gradbot.toml
```

### Testing
```bash
# Run all tests
cargo test

# Run library tests only
cargo test -p gradbot

# Run with output
cargo test -- --nocapture
```

### Building Python Bindings
```bash
cd gradbot_py
maturin develop  # Install in development mode
maturin build --release  # Build wheel
```

### Code Quality
```bash
# Format code
cargo fmt

# Lint (clippy)
cargo clippy

# Type check fast
cargo check
```

## Code Organization

### Source Structure
```
gradbot_lib/src/        # Core library
├── lib.rs               # Public API, GradbotClients, FlagshipVoice
├── multiplex.rs         # Conversation state machine and coordination
├── llm.rs               # LLM client with tool calling support
├── speech_to_text.rs    # STT client wrapper
├── text_to_speech.rs    # TTS client wrapper
├── system_prompt.rs     # Default prompt generation
├── encoder.rs           # Audio encoding (PCM, Opus, ulaw)
├── decoder.rs           # Audio decoding
└── utils.rs             # Utility functions

src/                     # Server binary
├── main.rs              # Entry point, CLI args, tracing
├── lib.rs               # Config loading, replace_env_vars
├── openai_server.rs     # OpenAI WebSocket server
├── twilio_server.rs     # Twilio WebSocket server
├── openai_protocol.rs   # OpenAI Realtime API events
└── twilio_protocol.rs   # Twilio event types

gradbot_py/src/       # Python bindings
└── lib.rs               # PyO3 bindings for gradbot
```

### Key Types (gradbot)
- `GradbotClients`: Shared TTS/STT/LLM clients for creating sessions
- `SessionConfig`: Voice, language, instructions, tools configuration
- `SessionInputHandle`/`SessionOutputHandle`: Async handles for session I/O
- `MsgIn`/`MsgOut`: Input audio/config and output audio/text/events
- `ToolDef`/`ToolCall`/`ToolCallHandle`: LLM function calling support
- `FlagshipVoice`: Pre-configured voice definitions
- `Lang`: Supported languages (En, Fr, Es, De, Pt)

### Key Types (server binary)
- `Config`: TOML-based configuration with environment variable substitution
- `Transport`: Enum for protocol selection (`WsOpenai`, `Twilio`)

## Naming Conventions & Style

### Code Style
- Rust 2024 edition
- `snake_case` for functions, variables, modules
- `PascalCase` for types, enums, structs
- Use `tracing::info!`, `tracing::debug!`, `tracing::error!` for logging
- Prefix unused parameters with `_` (e.g., `event_id: _`)

### Error Handling
- Nearly all functions return `Result<()>`, `Result<T>`, or `Result<Option<T>>`
- Use `anyhow::Result` for flexible error types
- Pattern match on `?` propagation is preferred

### Async Patterns
- All I/O is async (`async fn`)
- Use `#[tokio::main]` for entry point
- Use `tokio::sync::mpsc` for channels (`unbounded_channel` for fire-and-forget)
- Tasks are canceled via drop (no explicit abort needed)

## Configuration System

### TOML Config Files
Config files use `$VAR` syntax for environment variables and `$HOME` for home directory expansion. Example:
```toml
log_dir = "$HOME/tmp/tts-logs"
gradium_api_key = "$GRADIUM_API_KEY"
```

The `Config::load()` function:
1. Sets `CONFIG_DIR` environment var to config file's parent directory (unsafe)
2. Reads TOML file
3. Applies `replace_env_vars` to string fields (replaces `$VAR` with env values)

### Config Fields
- Required: `log_dir`, `addr`, `port`, `instance_name`, `gradium_api_key`, `gradium_base_url`, `transport`
- Optional: `llm_base_url`, `static_dir`, `max_completion_tokens`, `log_sessions` (defaults to `false`)

## Tracing Setup

The `tracing_init()` function:
- Creates daily rolling log files: `log.{instance_name}.{date}`
- Supports both JSON and text log formats via `LOG_AS_JSON` env var
- Logs to both file and stdout (unless `--silent` flag)
- Set log level via `--log` CLI arg (default: `info`)

## State Machine Details

### Conversational States
1. **Listening**: Collecting audio and transcribed text
2. **Flushing**: 0.8s flush window after `end_of_turn` from STT
3. **Processing**: Generating LLM response and TTS audio

### Interruption Handling
- In `Processing` state, if STT detects new speech, interrupts current generation
- Calls `LlmResponseStream::abort()` to stop LLM and TTS
- Returns to `Listening` state

### Timing & Synchronization
- All events have `time_s` timestamp relative to STT stream start
- `TimedQueue` module ensures audio/text events play at correct times
- Sample rate assumptions: STT Input 24kHz, TTS Output 48kHz

## Testing Patterns

### Test Location
Tests are inline in the source files using `#[cfg(test)]` modules.

### Current Tests
- `openai_protocol.rs`: `test_random_id()` verifies ID generation format

### Running Tests
From workspace root or gradbot directory:
```bash
cargo test -p gradbot
```

Note: Most integration tests are in `examples/` rather than unit tests.

## Important Gotchas

### 1. Unsafe Environment Variable Setting
`lib.rs:43-45` uses `unsafe { std::env::set_var(...) }` to set `CONFIG_DIR`. This allows config files to reference their directory via `$CONFIG_DIR` in TOML.

### 2. Workspace Dependencies
All dependencies are from the workspace (e.g., `async-openai = { workspace = true }`). Check workspace Cargo.toml for version constraints.

### 3. Static File Serving
If `static_dir` is configured, Axum serves static files with `ServeDir` and auto-appends `index.html` for directories.

### 4. Session Logging
When `log_sessions = true`, WebSocket sessions are logged as JSONL files:
- Pattern: `session_{timestamp}_{counter:06}.jsonl`
- Each line is a `TimedEvent` with `wall_time_s` (wall clock) and `time_s` (media time)
- Events include internal `multiplex::Event` types

### 5. Base64 Encoding
Audio data in OpenAI protocol is base64-encoded. Use:
```rust
use base64::engine::general_purpose:: STANDARD as B64;
let encoded = B64.encode(data);
let decoded = B64.decode(&encoded)?;
```

### 6. Audio Frame Sizes
- STT input: 1920 samples/frame at 24kHz (80ms)
- TTS output: 3840 samples/frame at 48kHz (80ms)

### 7. Word Buffering for LLM
In `llm.rs`, words are buffered and sent when space character is detected (split on `' '`). This is a simplification for streaming word-by-word output.

### 8. Client Examples
The `examples/gradbot-client.rs` demonstrates:
- WebSocket client connecting to OpenAI protocol
- Real-time audio streaming with simulated delays
- Input/Output sample rate handling and resampling
- Event parsing and logging

## Workspace Context

Gradbot is part of a multi-crate workspace:
- `common/`: Shared utilities used by gradbot (encoder, decoder, utils)
- Uses `gradium` crate for actual TTS/STT client implementation
- Workspace root Cargo.toml defines all dependency versions

When adding dependencies:
1. Check if already in workspace dependencies
2. Add with `{ workspace = true }` if present, or specify version directly

## Development Workflow

1. **Make changes** to source files
2. **Run `cargo check -p gradbot`** to catch type errors quickly
3. **Run `cargo clippy -p gradbot`** to fix warnings
4. **Run `cargo test -p gradbot`** to verify tests pass
5. **If you changed gradbot_py or gradbot**: run `uv sync --reinstall-package gradbot` in the demo directory to pick up changes
6. **Optional**: Run example client to test manually:
   ```bash
   cargo run -p gradbot --bin gradbot -- --config configs/gradbot.toml
   ```

## Common Patterns

### Protocol Implementation
When adding new events to OpenAI protocol:
1. Add enum variant to `ClientEvent` or `ServerEvent`
2. Implement `random_event_id()` method or similar
3. Update `WebSocketReceiver::recv()` or `LoggedSender` methods
4. Add tests for serialization/deserialization

### Audio Processing Pipeline
Audio flows as:
1. Raw Vec<u8> → `gradium_common::decoder` → PCM f32
2. PCM → STT client → text stream
3. Text → LLM client → word stream
4. Words → TTS client → PCM stream
5. PCM → `gradium_common::encoder` → encoded bytes
6. Bytes → WebSocket (base64)

### State Transitions
The `multiplex::Session` state machine uses conditional matching:
```rust
match &mut state {
    State::Listening { since_s, texts } => { /* ... */ }
    State::Flushing { since_s, texts, flush_duration_s } => { /* ... */ }
    State::Processing { since_s, _jh } => { /* ... */ }
}
```

Always update `state` fields (e.g., `texts` vector) in place using `std::mem::take` or direct modifications.

## Logging Best Practices

- Use `tracing::info!` for important lifecycle events (startup, shutdown, errors)
- Use `tracing::debug!` for detailed flow and messages
- Use `?` syntax to include error details in logs
- Log structured data with field notation: `tracing::info!(?msg, "received event")`

## Dependency Management

### External Crates
- `axum`: Async web framework with WebSocket support
- `async-openai`: OpenAI API client wrapper
- `tokio-tungstenite`: WebSocket client/server
- `gradium` & `gradium-common`: Internal ML audio processing
- `anyhow`: Error handling
- `tracing` & `tracing-subscriber`: Structured logging

### Using Workspace Crates
```rust
// In lib.rs
pub use crate::module::Type;

// When referencing from another crate
use gradbot::Config;
```
