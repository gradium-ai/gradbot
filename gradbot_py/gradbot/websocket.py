"""FastAPI WebSocket bridge for gradbot voice AI sessions."""

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

import fastapi

from . import _gradbot as gradbot
from . import config as config_lib
from . import schemas

logger = logging.getLogger("gradbot.websocket")

ConfigCallback = Callable[
    [dict],
    Awaitable[gradbot.SessionConfig] | gradbot.SessionConfig,
]


class ToolHandle:
    """Wrapper around ToolCallHandlePy with send_json and sanitized args."""

    def __init__(
        self,
        handle: gradbot.ToolCallHandlePy,
        tool_call: gradbot.ToolCallInfo,
    ):
        self._handle = handle
        self.name = tool_call.tool_name
        try:
            self.args = (
                schemas.sanitize(json.loads(tool_call.args_json))
                if tool_call.args_json
                else {}
            )
        except (json.JSONDecodeError, TypeError):
            self.args = {}

    async def send(self, result_json: str) -> None:
        await self._handle.send(result_json)

    async def send_json(self, obj: Any) -> None:
        await self._handle.send(json.dumps(obj))

    async def send_error(self, error_message: str) -> None:
        await self._handle.send_error(error_message)


def _ensure_async(fn: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """Wrap a sync function to be awaitable."""
    if inspect.iscoroutinefunction(fn):
        return fn

    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


async def handle_session(
    websocket: fastapi.WebSocket,
    *,
    on_start: ConfigCallback,
    on_config: ConfigCallback | None = None,
    on_tool_call: Callable[..., Awaitable[None]] | None = None,
    config: config_lib.Config | None = None,
    run_kwargs: dict | None = None,
    input_format: gradbot.AudioFormat = gradbot.AudioFormat.OggOpus,
    output_format: gradbot.AudioFormat = gradbot.AudioFormat.OggOpus,
    debug: bool = False,
) -> None:
    """Handle a full WebSocket voice-chat session.

    Pass ``config`` (a ``Config``) to set ``run_kwargs``,
    ``output_format``, and ``debug`` automatically. Or pass
    them individually for custom setups.

    Protocol:
    - Client sends JSON ``{"type": "start", ...}``.
    - Client sends binary frames with audio data.
    - Client sends JSON ``{"type": "config", ...}``
      to reconfigure mid-session.
    - Client sends JSON ``{"type": "stop"}``.
    - Server sends transcripts, events, audio, timing.
    """
    if config is not None:
        run_kwargs = config.client_kwargs
        output_format = config.audio_format
        debug = config.debug

    on_start = _ensure_async(on_start)
    if on_config is not None:
        on_config = _ensure_async(on_config)

    pending_tool_tasks: set[asyncio.Task] = set()

    async def send_error(exc: Exception) -> None:
        try:
            text = str(exc) if debug else "An error occurred"
            await websocket.send_json(schemas.Error(message=text).model_dump())
        except Exception:
            ...

    async def handle_input(raw: dict) -> bool:
        """Handle one input frame. Returns False to stop."""
        if "bytes" in raw:
            await input_handle.send_audio(raw["bytes"])
            return True

        if "text" not in raw:
            return True

        data = json.loads(raw["text"])
        msg_type = data.get("type")
        if msg_type == "stop":
            return False

        if msg_type == "config" and on_config is not None:
            try:
                new_cfg = await on_config(data)
                await input_handle.send_config(new_cfg)
            except RuntimeError as exc:
                await send_error(exc)
        return True

    async def handle_output(msg: gradbot.MsgOut) -> None:
        if msg.msg_type == "tool_call":
            if on_tool_call is not None:
                handle = ToolHandle(msg.tool_call_handle, msg.tool_call)

                async def _safe_tool_call(h=handle):
                    try:
                        await on_tool_call(h, input_handle, websocket)
                    except Exception as exc:
                        logger.exception("Tool %s failed", h.name)
                        try:
                            await h.send_error(str(exc))
                        except Exception:
                            pass

                task = asyncio.create_task(_safe_tool_call())
                pending_tool_tasks.add(task)
                task.add_done_callback(pending_tool_tasks.discard)
            return

        schema = schemas.from_msg(msg)
        if schema is None:
            return

        await websocket.send_json(schema.model_dump())
        if msg.msg_type == "audio":
            await websocket.send_bytes(msg.data)

    async def input_loop() -> None:
        while not stop_event.is_set():
            try:
                raw = await websocket.receive()
                if not await handle_input(raw):
                    stop_event.set()
                    await input_handle.close()
                    break
            except (
                fastapi.WebSocketDisconnect,
                RuntimeError,
            ):
                stop_event.set()
                await input_handle.close()
                break
            except Exception:
                logger.exception("input loop error")
                stop_event.set()
                break

    async def output_loop() -> None:
        while not stop_event.is_set():
            try:
                msg = await output_handle.receive()
                if msg is None:
                    break
                await handle_output(msg)
            except Exception as exc:
                logger.exception("output loop error")
                await send_error(exc)
                break

    await websocket.accept()
    try:
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        try:
            config = await on_start(start_msg)
        except RuntimeError as exc:
            logger.error("on_start error: %s", exc)
            await websocket.close(code=4001, reason=str(exc))
            return

        input_handle, output_handle = await gradbot.run(
            **(run_kwargs or {}),
            session_config=config,
            input_format=input_format,
            output_format=output_format,
        )

        stop_event = asyncio.Event()
        logger.info("session started")
        results = await asyncio.gather(
            output_loop(),
            input_loop(),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("task %d raised: %s", i, r)

    except Exception as exc:
        logger.exception("session error")
        await send_error(exc)
    finally:
        for t in pending_tool_tasks:
            t.cancel()
        if pending_tool_tasks:
            await asyncio.gather(
                *pending_tool_tasks,
                return_exceptions=True,
            )
        try:
            await websocket.close()
        except Exception:
            pass
