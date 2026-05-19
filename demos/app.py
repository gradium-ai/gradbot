"""Combined app that mounts all demos under /<demo_name>/."""

import contextlib
import importlib
import sys
from pathlib import Path

from fastapi import FastAPI

DEMOS_DIR = Path(__file__).parent

# Discover and import each demo before constructing the parent app, so
# the parent's lifespan can chain into each child's lifespan (FastAPI
# does not propagate lifespans through `app.mount(...)`).
demo_names = sorted(
    d.name
    for d in DEMOS_DIR.iterdir()
    if d.is_dir() and (d / "main.py").exists()
)

_demos: list[tuple[str, FastAPI]] = []
for name in demo_names:
    demo_path = DEMOS_DIR / name
    sys.path.insert(0, str(demo_path))
    try:
        mod = importlib.import_module(f"{name}.main")
        demo_app = getattr(mod, "app", None)
        if demo_app is not None:
            _demos.append((name, demo_app))
    except Exception as e:
        print(f"Warning: could not load demo '{name}': {e}")
    finally:
        sys.path.pop(0)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        for _, demo_app in _demos:
            await stack.enter_async_context(
                demo_app.router.lifespan_context(demo_app)
            )
        yield


app = FastAPI(title="Gradbot Demos", lifespan=_lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


for name, demo_app in _demos:
    app.mount(f"/{name}", demo_app)
