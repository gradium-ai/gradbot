# Simple Chat Demo

A real-time voice chat demo using gradbot.

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
export LLM_API_KEY=your_llm_key  # or use LLM_BASE_URL + LLM_API_KEY for other providers

# Run the server
uv run uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser.

## Features

- Select from 14 flagship voices across 5 languages
- Customize the AI system prompt
- **Change voice and prompt mid-conversation** without restarting
- Real-time voice conversation
- Live transcript display
