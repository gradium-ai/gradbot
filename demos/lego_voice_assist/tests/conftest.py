"""Make `main` importable for unit tests without the native `gradbot`
extension or real API keys.

`main.py` does `import gradbot` at module scope and calls
`gradbot.init_logging()` / `gradbot.config.from_env()` before any of its pure
helper functions are defined, so importing it at all requires the compiled
Rust binding. That binding isn't needed by the logic these tests exercise
(grid math, speech phrasing, cell/box parsing), so stub the module instead of
requiring contributors to build the Rust workspace just to run `pytest` here.
"""

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

if "gradbot" not in sys.modules:
    gradbot_stub = types.ModuleType("gradbot")
    gradbot_stub.init_logging = lambda: None

    config_stub = types.ModuleType("gradbot.config")
    config_stub.from_env = lambda *args, **kwargs: None
    gradbot_stub.config = config_stub

    routes_stub = types.ModuleType("gradbot.routes")
    routes_stub.setup = lambda *args, **kwargs: None
    gradbot_stub.routes = routes_stub

    sys.modules["gradbot"] = gradbot_stub
    sys.modules["gradbot.config"] = config_stub
    sys.modules["gradbot.routes"] = routes_stub
