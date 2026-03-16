"""Type stubs for gradbot.fastapi module."""

from pathlib import Path
from typing import Any, Awaitable, Callable

from gradbot._gradbot import AudioFormat, SessionConfig

async def websocket_chat_handler(
    websocket: Any,
    *,
    on_start: Callable[[dict[str, Any]], Awaitable[SessionConfig] | SessionConfig],
    on_config: Callable[[dict[str, Any]], Awaitable[SessionConfig] | SessionConfig] | None = None,
    on_tool_call: Callable[..., Awaitable[None]] | None = None,
    run_kwargs: dict[str, Any] | None = None,
    input_format: AudioFormat = AudioFormat.OggOpus,
    output_format: AudioFormat = AudioFormat.OggOpus,
    debug: bool = False,
) -> None: ...

def setup_demo_routes(
    app: Any,
    *,
    static_dir: Path | str | None = None,
    use_pcm: bool = False,
    voices: bool = False,
) -> None: ...
