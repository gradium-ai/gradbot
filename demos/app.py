"""Combined app that mounts all demos under /<demo_name>/."""

import importlib
import sys
from pathlib import Path

from fastapi import FastAPI

DEMOS_DIR = Path(__file__).parent

app = FastAPI(title="Gradbot Demos")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Discover and mount each demo
demo_names = sorted(
    d.name
    for d in DEMOS_DIR.iterdir()
    if d.is_dir() and (d / "main.py").exists()
)

for name in demo_names:
    demo_path = DEMOS_DIR / name
    # Add demo dir to sys.path so its main.py can resolve local imports
    sys.path.insert(0, str(demo_path))
    try:
        mod = importlib.import_module(f"{name}.main")
        demo_app = getattr(mod, "app", None)
        if demo_app is not None:
            app.mount(f"/{name}", demo_app)
    except Exception as e:
        print(f"Warning: could not load demo '{name}': {e}")
    finally:
        sys.path.pop(0)
