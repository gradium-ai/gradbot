# Simple Chat Demo

A minimal real-time voice chat demo using gradbot.

## Setup

```bash
cd gradbot/demos/simple_chat
uv sync
```

This will build gradbot from source using maturin.

> **After changing gradbot Rust code**, re-run with `uv sync --reinstall-package gradbot` to rebuild the package. A plain `uv sync` won't pick up changes if the version hasn't changed.

## Run

```bash
# Set your API keys
export GRADIUM_API_KEY=your_key_here
export LLM_API_KEY=your_llm_key
export LLM_BASE_URL=your_llm_endpoint

# Run the server
uv run uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser.

## Features

- Minimal FastAPI + WebSocket voice chat example
- Fixed time-traveller system prompt defined in `main.py`
- Choose a voice from the available Gradium catalog voices before starting
- Real-time voice conversation
- Live transcript display
- Echo cancellation toggle in the UI
