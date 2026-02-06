# Simple Chat Demo

A real-time voice chat demo using pygradbot.

## Setup

```bash
cd gradbot/demos/simple_chat
uv sync
```

This will build pygradbot from source using maturin.

## Run

```bash
# Set your API keys
export GRADIUM_API_KEY=your_key_here
export OPENAI_API_KEY=your_openai_key  # or use LLM_BASE_URL for other providers

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
