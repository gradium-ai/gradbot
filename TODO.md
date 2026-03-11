# Gradbot TODO

## Python OpenAI Server Bindings

### Python-extensible OpenAI-compatible server

Create Python bindings for the OpenAI server that allow Python code to:
1. Handle tool calls with custom Python functions (returning results + config updates)
2. Customize session configuration per-connection (returning a list of configs)
3. Add middleware/hooks for request processing

**Python API**:
```python
from gradbot.server import OpenAIServer, ToolHandler, ConfigMapper
from gradbot import SessionConfig, ToolCall, ToolResult

class MyToolHandler(ToolHandler):
    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        """
        Handle a tool call from the LLM.
        Returns the result plus any config updates (e.g., voice changes).
        """
        if call.name == "get_weather":
            city = call.arguments["city"]
            weather = await fetch_weather(city)
            return ToolResult(
                call_id=call.id,
                result={"weather": weather},
                config_updates=[]  # No config changes
            )

        if call.name == "change_voice":
            voice_name = call.arguments["voice"]
            voice_id = VOICE_MAP.get(voice_name)
            return ToolResult(
                call_id=call.id,
                result={"status": "ok", "voice": voice_name},
                config_updates=[SessionConfig(voice_id=voice_id)]
            )

        raise UnknownTool(call.name)

class MyConfigMapper(ConfigMapper):
    async def map_config(self, session_update: dict) -> list[SessionConfig]:
        """
        Map an incoming session update to a list of SessionConfigs.

        Returns a list because:
        - Empty list: reject/ignore the update
        - Single item: standard 1:1 mapping
        - Multiple items: apply configs in sequence (e.g., base + user overrides)
        """
        user_id = session_update.get("user_id")
        if not user_id:
            return []  # Reject anonymous sessions

        # Look up user preferences
        user_prefs = await db.get_user_prefs(user_id)

        # Base config
        base = SessionConfig(
            instructions=SYSTEM_PROMPT,
            language="en",
            tools=[weather_tool, voice_change_tool],
        )

        # User-specific overrides
        user_config = SessionConfig(
            voice_id=user_prefs.preferred_voice,
            instructions=user_prefs.custom_instructions,
        )

        return [base, user_config]  # Applied in order

server = OpenAIServer(
    tts_client=tts,
    stt_client=stt,
    llm=llm,
    tool_handler=MyToolHandler(),
    config_mapper=MyConfigMapper(),
)

server.run(host="0.0.0.0", port=8080)
```

**Key design decisions**:
- `ConfigMapper.map_config()` returns `list[SessionConfig]` not a single config
  - Empty list = reject/ignore
  - Multiple configs = apply in sequence (later overrides earlier)
- `ToolHandler.handle_tool_call()` returns `ToolResult` which includes:
  - The actual result for the LLM
  - A list of `SessionConfig` updates to apply (e.g., voice change)
- This allows tools to modify session state as a side effect

**Implementation approach**:
1. Create Rust traits for `ToolHandler` and `ConfigMapper` with pyo3 bridge
2. Allow Python classes to implement these traits
3. Server calls into Python for tool execution and config mapping
4. Use `pyo3-asyncio` for async Python calls

**Considerations**:
- GIL management for concurrent requests
- Error handling across Rust/Python boundary
- Performance impact of Python callbacks
- Could use a thread pool for Python execution

---

## Python Packaging

### PyPI distribution

Package the Python bindings for easy installation via pip.

**Package structure**:
```
gradbot_py/
├── pyproject.toml
├── Cargo.toml           # pyo3 crate
├── src/
│   └── lib.rs           # pyo3 bindings
├── python/
│   └── gradbot/
│       ├── __init__.py  # Re-exports
│       ├── py.typed     # PEP 561 marker
│       └── _gradbot.pyi  # Type stubs
├── README.md
└── examples/
    ├── minimal.py
    └── with_tools.py
```

**Type stubs (`_gradbot.pyi`)**:
```python
from typing import Optional, List, Tuple
from enum import Enum

class Lang(Enum):
    En = ...
    Fr = ...
    Es = ...
    De = ...
    Pt = ...

class Gender(Enum):
    Masculine = ...
    Feminine = ...

class Country(Enum):
    Us = ...
    Gb = ...
    Fr = ...
    De = ...
    Mx = ...
    Es = ...
    Br = ...

class AudioFormat(Enum):
    OggOpus = ...
    Pcm = ...
    Ulaw = ...

class FlagshipVoice:
    name: str
    voice_id: str
    language: Lang
    country: Country
    gender: Gender
    description: str

class ToolDef:
    name: str
    description: str
    parameters_json: str
    def __init__(self, name: str, description: str, parameters_json: str) -> None: ...

class SessionConfig:
    voice_id: Optional[str]
    instructions: Optional[str]
    language: Lang
    assistant_speaks_first: bool
    silence_timeout_s: float
    tools: List[ToolDef]
    def __init__(
        self,
        voice_id: Optional[str] = None,
        instructions: Optional[str] = None,
        language: Lang = Lang.En,
        assistant_speaks_first: bool = True,
        silence_timeout_s: float = 5.0,
        tools: List[ToolDef] = [],
    ) -> None: ...

class ToolCallInfo:
    call_id: str
    tool_name: str
    args_json: str

class ToolCallHandlePy:
    async def send(self, result_json: str) -> None: ...
    async def send_error(self, error_message: str) -> None: ...

class Event:
    event_type: str
    data: Optional[object]

class MsgOut:
    msg_type: str
    data: Optional[bytes]
    text: Optional[str]
    start_s: Optional[float]
    stop_s: Optional[float]
    time_s: Optional[float]
    event: Optional[Event]
    tool_call: Optional[ToolCallInfo]
    tool_call_handle: Optional[ToolCallHandlePy]

class SessionInputHandle:
    async def send_audio(self, data: bytes) -> None: ...
    async def send_config(self, config: SessionConfig) -> None: ...
    async def close(self) -> None: ...

class SessionOutputHandle:
    async def receive(self) -> Optional[MsgOut]: ...

class GradbotClients:
    async def start_session(
        self,
        initial_config: Optional[SessionConfig] = None,
        input_format: AudioFormat = AudioFormat.Pcm,
        output_format: AudioFormat = AudioFormat.OggOpus,
    ) -> Tuple[SessionInputHandle, SessionOutputHandle]: ...

def init_logging() -> None: ...
def flagship_voices() -> List[FlagshipVoice]: ...
def flagship_voice(name: str) -> FlagshipVoice: ...
async def create_clients(
    gradium_api_key: Optional[str] = None,
    gradium_base_url: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model_name: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
) -> GradbotClients: ...
async def run(
    gradium_api_key: Optional[str] = None,
    gradium_base_url: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model_name: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
    session_config: Optional[SessionConfig] = None,
    input_format: AudioFormat = AudioFormat.Pcm,
    output_format: AudioFormat = AudioFormat.OggOpus,
) -> Tuple[SessionInputHandle, SessionOutputHandle]: ...
```

**Build & publish workflow**:
```yaml
# .github/workflows/python-publish.yml
name: Publish Python Package

on:
  release:
    types: [published]

jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.9", "3.10", "3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - uses: PyO3/maturin-action@v1
        with:
          command: build
          args: --release -o dist

  publish:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
```

**Installation**:
```bash
pip install gradbot
```
