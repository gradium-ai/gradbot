"""FastAPI WebSocket bridge for gradbot voice AI sessions.

Provides a reusable handler that eliminates ~100 lines of boilerplate per demo.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import gradbot

logger = logging.getLogger("gradbot.fastapi")


async def _maybe_await(result):
    """Await the result if it's a coroutine, otherwise return it directly."""
    if inspect.isawaitable(result):
        return await result
    return result


async def websocket_chat_handler(
    websocket: WebSocket,
    *,
    on_start: Callable[[dict], Awaitable[gradbot.SessionConfig] | gradbot.SessionConfig],
    on_config: Callable[[dict], Awaitable[gradbot.SessionConfig] | gradbot.SessionConfig] | None = None,
    on_tool_call: Callable[..., Awaitable[None]] | None = None,
    run_kwargs: dict | None = None,
    input_format: gradbot.AudioFormat = gradbot.AudioFormat.OggOpus,
    output_format: gradbot.AudioFormat = gradbot.AudioFormat.OggOpus,
    debug: bool = False,
) -> None:
    """Handle a full WebSocket voice-chat session with gradbot.

    Protocol:
    - Client sends JSON ``{"type": "start", ...}`` to begin a session.
    - Client sends binary frames with audio data.
    - Client sends JSON ``{"type": "config", ...}`` to reconfigure mid-session.
    - Client sends JSON ``{"type": "stop"}`` to end the session.
    - Server sends JSON transcripts, events, audio-timing, and binary audio.

    Parameters
    ----------
    websocket:
        The FastAPI WebSocket connection.
    on_start:
        Called with the start message dict; must return a ``SessionConfig``.
    on_config:
        Called with config-change message dicts; must return a ``SessionConfig``.
        If *None*, mid-session config changes are ignored.
    on_tool_call:
        Called with ``(tool_call_info, tool_call_handle, input_handle, websocket)``
        when the LLM invokes a tool.  If *None*, tool-call messages are silently
        ignored.  The extra arguments allow tool handlers to reconfigure the
        session (via ``input_handle.send_config()``) or send custom messages to
        the client (via ``websocket.send_json()``).
    run_kwargs:
        Extra keyword arguments forwarded to ``gradbot.run()``.
    input_format:
        Audio format expected from the client.
    output_format:
        Audio format sent to the client.
    debug:
        If *True*, raw error messages are forwarded to the client.
    """
    await websocket.accept()

    pending_tool_tasks: set[asyncio.Task] = set()

    try:
        # ---- Wait for start message ----
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        try:
            config = await _maybe_await(on_start(start_msg))
        except RuntimeError as exc:
            logger.error("on_start error: %s", exc)
            await websocket.close(code=4001, reason=str(exc))
            return

        # ---- Start gradbot session ----
        input_handle, output_handle = await gradbot.run(
            **(run_kwargs or {}),
            session_config=config,
            input_format=input_format,
            output_format=output_format,
        )

        stop_event = asyncio.Event()
        logger.debug("session started, entering loops")

        # ---- Output loop ----
        async def _output_loop():
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        logger.debug("output loop: received None, ending")
                        break

                    if msg.msg_type == "audio":
                        await websocket.send_json({
                            "type": "audio_timing",
                            "start_s": msg.start_s,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                            "interrupted": msg.interrupted,
                        })
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                    elif msg.msg_type == "tool_call":
                        if on_tool_call is not None:
                            task = asyncio.create_task(
                                on_tool_call(msg.tool_call, msg.tool_call_handle,
                                             input_handle, websocket)
                            )
                            pending_tool_tasks.add(task)
                            task.add_done_callback(pending_tool_tasks.discard)

                except Exception as exc:
                    logger.exception("output loop error")
                    try:
                        await _send_error(websocket, exc, debug)
                    except Exception:
                        pass
                    break

        # ---- Input loop ----
        async def _input_loop():
            while not stop_event.is_set():
                try:
                    raw = await websocket.receive()

                    if "text" in raw:
                        data = json.loads(raw["text"])
                        msg_type = data.get("type")

                        if msg_type == "stop":
                            stop_event.set()
                            await input_handle.close()
                            break

                        elif msg_type == "config" and on_config is not None:
                            try:
                                new_config = await _maybe_await(on_config(data))
                                await input_handle.send_config(new_config)
                            except RuntimeError as exc:
                                await _send_error(websocket, exc, debug)

                    elif "bytes" in raw:
                        await input_handle.send_audio(raw["bytes"])

                except WebSocketDisconnect:
                    stop_event.set()
                    await input_handle.close()
                    break
                except Exception:
                    logger.exception("input loop error")
                    stop_event.set()
                    break

        results = await asyncio.gather(_output_loop(), _input_loop(), return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("task %d raised: %s", i, r)

    except Exception as exc:
        logger.exception("session error")
        try:
            await _send_error(websocket, exc, debug)
        except Exception:
            pass
    finally:
        for t in pending_tool_tasks:
            t.cancel()
        if pending_tool_tasks:
            await asyncio.gather(*pending_tool_tasks, return_exceptions=True)
        try:
            await websocket.close()
        except Exception:
            pass


async def _send_error(websocket: WebSocket, exc: Exception, debug: bool) -> None:
    """Send an error message to the client."""
    await websocket.send_json({
        "type": "error",
        "message": str(exc) if debug else "An error occurred during the session",
    })


def setup_demo_routes(
    app,
    *,
    static_dir: Path | str | None = None,
    use_pcm: bool = False,
    voices: bool = False,
) -> None:
    """Register standard demo routes on a FastAPI app.

    Parameters
    ----------
    app:
        The FastAPI application instance.
    static_dir:
        Path to the static files directory. If provided, ``GET /`` serves
        ``index.html`` from this directory and ``/static`` is mounted.
    use_pcm:
        Value returned by ``GET /api/audio-config``.
    voices:
        If *True*, registers ``GET /api/voices`` returning
        :func:`gradbot.voices_json`.
    """
    if static_dir is not None:
        static_dir = Path(static_dir)

    @app.get("/api/audio-config")
    async def audio_config():
        return JSONResponse(content={"pcm": use_pcm})

    if voices:
        _voices_response = {"voices": gradbot.voices_json()}

        @app.get("/api/voices")
        async def list_voices():
            return JSONResponse(content=_voices_response)

    if static_dir is not None:
        if static_dir.exists():
            app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")

        @app.get("/")
        async def index():
            index_path = static_dir / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
            return JSONResponse(content={"error": "Frontend not found"}, status_code=404)
