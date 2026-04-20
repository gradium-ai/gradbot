# Gradbot Demos

Each subfolder with a `main.py` is a standalone voice AI demo powered by [gradbot](../gradbot_py/).

## Structure

```
demos/
  app.py                 # Combined launcher — discovers and mounts demos at /<demo_name>
  config.example.yaml    # Example shared config for the combined launcher
  simple_chat/           # Minimal voice chat reference
  fantasy_shop/          # Voice haggling game with tools and persona swaps
  business_bank/         # Voice banking workflow with multi-step tools
  hotel/                 # Hotel booking agent with deferred search tools
  restaurant_ordering/   # Multilingual restaurant ordering agent
  npc_3d_game/           # Three.js game with multiple voice sessions and direct TTS
```

## Running locally

```bash
cd demos
uv sync
uv run uvicorn app:app --reload --port 8000
```

Each demo is served at `http://localhost:8000/<demo_name>/`.
There is no combined index page, so open a demo path directly, for example `http://localhost:8000/simple_chat/`.

## Adding a new demo

1. Create a folder: `demos/my_demo/`
2. Add `main.py` with a FastAPI `app` instance
3. Add `static/index.html` if the demo has a browser frontend
4. It's automatically discovered and mounted by `app.py`

See `simple_chat/` for a minimal example.

## Configuration

Demos do not all load config the same way:

1. Demos that call `gradbot.config.from_env()` read from `CONFIG_DIR` if set, otherwise from the current working directory.
2. Demos that call `gradbot.config.load(Path(__file__).parent)` look for `<demo>/config.yaml` first and fall back to `demos/config.yaml`.
3. Environment variables (`LLM_MODEL`, `GRADIUM_API_KEY`, etc.) override YAML settings.

If you run the combined launcher from `demos/`, copying `config.example.yaml` to `demos/config.yaml` is the usual starting point.
