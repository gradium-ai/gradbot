"""Combined app that mounts all demos under /<demo_name>/."""

import importlib
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DEMOS_DIR = Path(__file__).parent

app = FastAPI(title="Gradbot Demos")

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


@app.get("/", response_class=HTMLResponse)
async def index():
    items = "\n".join(
        f'<li><a href="{name}/">{name.replace("_", " ").title()}</a></li>'
        for name in demo_names
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Gradbot Demos</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #f1f5f9;
           display: flex; justify-content: center; padding: 4rem; }}
    ul {{ list-style: none; padding: 0; }}
    li {{ margin: 0.75rem 0; }}
    a {{ color: #818cf8; text-decoration: none; font-size: 1.2rem; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div>
    <h1>Gradbot Demos</h1>
    <ul>{items}</ul>
  </div>
</body>
</html>"""
