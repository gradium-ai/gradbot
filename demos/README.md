# Gradbot Demos

Each subfolder is a standalone voice AI demo powered by [gradbot](../gradbot_py/).

## Structure

```
demos/
  app.py              # Entrypoint — discovers and mounts all demos
  config.example.yaml # Example config (copy to config.yaml)
  simple_chat/        # Demo: time-traveller voice chat
  chick_fil_a/        # Demo: Chick-fil-A ordering agent
  hotel/              # Demo: hotel concierge
  ...
```

## Running locally

```bash
cd demos
uv sync
uv run uvicorn app:app --reload --port 8000
```

Each demo is served at `http://localhost:8000/<demo_name>/`.

## Adding a new demo

1. Create a folder: `demos/my_demo/`
2. Add `main.py` with a FastAPI `app` instance
3. Add `static/index.html` for the frontend
4. It's automatically discovered and mounted by `app.py`

See `simple_chat/` for a minimal example.

## Configuration

Demos load config from (in order):
1. `demos/config.yaml` (shared defaults)
2. `demos/<demo>/config.yaml` (per-demo overrides)
3. Environment variables (`LLM_MODEL`, `GRADIUM_API_KEY`, etc.)

Copy `config.example.yaml` to `config.yaml` to get started.
