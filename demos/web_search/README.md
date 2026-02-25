# Web Search Demo

A voice-powered web search assistant. Ask a question by voice, the AI searches the web using DuckDuckGo, results appear in a side panel, and the AI discusses what it found.

## Setup

```bash
cd gradbot/demos/web_search
uv sync
```

This will build pygradbot from source using maturin.

> **After changing gradbot Rust code**, re-run with `uv sync --reinstall-package pygradbot` to rebuild the package. A plain `uv sync` won't pick up changes if the version hasn't changed.

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

- Voice-powered web search using DuckDuckGo
- Search results appear in a side panel with clickable links
- AI discusses and summarizes findings
- Async tool pattern: AI talks while search runs
- Two agent voices to choose from
