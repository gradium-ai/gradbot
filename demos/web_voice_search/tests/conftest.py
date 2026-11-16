"""Run MCP and Gradbot protocol fixtures over real local transports."""

import asyncio
import contextlib
import importlib
import json
import os
import pathlib
import socket
import sys
import threading
import time

import fastapi
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture
def demo(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("LLM_", "GRADIUM_", "GRADBOT_", "PARALLEL_", "KEENABLE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "parallel")
    import gradbot

    gradbot.config.load.cache_clear()
    import main

    return importlib.reload(main)


@pytest.fixture
def transport_server():
    observed = {"requests": [], "config": None, "tool_result": None}
    slow_release = asyncio.Event()
    mcp = FastMCP(
        "Search fixture",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
    )

    @mcp.tool()
    async def web_search(objective: str, search_queries: list[str], session_id: str):
        observed["arguments"] = dict(
            objective=objective, search_queries=search_queries, session_id=session_id
        )
        if objective == "slow":
            observed["slow_loop"] = asyncio.get_running_loop()
            await slow_release.wait()
        if objective == "error":
            raise ValueError("Search fixture failed")
        if objective == "empty":
            return {"results": []}
        return {
            "results": [
                {
                    "url": "https://www.example.org/france",
                    "title": "France",
                    "excerpts": ["Paris is the capital of France."],
                },
                {
                    "url": "https://www.example.org/france",
                    "title": "Duplicate",
                    "excerpts": [],
                },
                {
                    "url": "https://example.org/more",
                    "title": None,
                    "excerpts": ["More context."],
                },
            ]
        }

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = fastapi.FastAPI(lifespan=lifespan)
    app.mount("/mcp", mcp.streamable_http_app())

    @app.middleware("http")
    async def observe(request, call_next):
        if request.method == "POST":
            data = json.loads(await request.body())
            observed["requests"].append(
                {
                    "method": data.get("method"),
                    "params": data.get("params"),
                    "user_agent": request.headers.get("user-agent"),
                    "authorization": request.headers.get("authorization"),
                }
            )
        return await call_next(request)

    @app.websocket("/backend")
    async def backend(ws: fastapi.WebSocket):
        await ws.accept()
        observed["config"] = await asyncio.wait_for(ws.receive_json(), 10)
        await ws.send_json({"type": "session.config.applied", "pinned_fields": []})
        await ws.send_json(
            {
                "type": "tool_call",
                "call_id": "search-1",
                "tool_name": "web_search",
                "args": {"query": "What is the capital of France?"},
            }
        )
        observed["tool_result"] = await asyncio.wait_for(ws.receive_json(), 10)
        result = observed["tool_result"]["result"]
        # Protocol fixture echoes the real tool context as the final answer.
        await ws.send_json(
            {
                "type": "tts_text",
                "text": result["results_summary"],
                "start_s": 0.0,
                "stop_s": 1.0,
                "turn_idx": 0,
            }
        )
        with contextlib.suppress(Exception):
            await ws.receive()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}", observed
    if "slow_loop" in observed:
        observed["slow_loop"].call_soon_threadsafe(slow_release.set)
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
