"""Tests for the WebSocket session handler (websocket)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import fastapi
import fastapi.testclient
import pytest

import gradbot


# ── ToolHandle unit tests ───────────────────────────────────


def _mock_tool_call(name="test_tool", args_json='{"key": "val"}'):
    tc = MagicMock()
    tc.tool_name = name
    tc.args_json = args_json
    return tc


def test_tool_handle_parses_args():
    handle = gradbot.ToolHandle(AsyncMock(), _mock_tool_call())
    assert handle.name == "test_tool"
    assert handle.args == {"key": "val"}


def test_tool_handle_sanitizes_args():
    tc = _mock_tool_call(args_json='{"k": "<|junk|>clean"}')
    handle = gradbot.ToolHandle(AsyncMock(), tc)
    assert handle.args == {"k": "clean"}


def test_tool_handle_empty_args():
    tc = _mock_tool_call(args_json=None)
    handle = gradbot.ToolHandle(AsyncMock(), tc)
    assert handle.args == {}


def test_tool_handle_malformed_json():
    tc = _mock_tool_call(args_json="not json{")
    handle = gradbot.ToolHandle(AsyncMock(), tc)
    assert handle.args == {}


@pytest.mark.asyncio
async def test_tool_handle_send_json():
    inner = AsyncMock()
    handle = gradbot.ToolHandle(inner, _mock_tool_call())
    await handle.send_json({"result": "ok"})
    inner.send.assert_called_once_with('{"result": "ok"}')


@pytest.mark.asyncio
async def test_tool_handle_send_error():
    inner = AsyncMock()
    handle = gradbot.ToolHandle(inner, _mock_tool_call())
    await handle.send_error("something broke")
    inner.send_error.assert_called_once_with("something broke")


# ── WebSocket session tests ─────────────────────────────────


def _make_app(on_start=None, on_tool_call=None):
    """Build a tiny FastAPI app that uses handle_session."""
    app = fastapi.FastAPI()

    if on_start is None:
        on_start = lambda msg: gradbot.SessionConfig()

    @app.websocket("/ws")
    async def ws(websocket: fastapi.WebSocket):
        await gradbot.websocket.handle_session(
            websocket,
            on_start=on_start,
            on_tool_call=on_tool_call,
        )

    return app


def _mock_output_handle(messages):
    """Mock output handle that yields messages then None."""
    it = iter(messages)
    handle = AsyncMock()
    handle.receive = AsyncMock(side_effect=lambda: next(it, None))
    return handle


def _mock_input_handle():
    handle = AsyncMock()
    handle.send_audio = AsyncMock()
    handle.send_config = AsyncMock()
    handle.close = AsyncMock()
    return handle


def _text_msg(text, msg_type="tts_text"):
    """Create a mock MsgOut for text."""
    msg = MagicMock()
    msg.msg_type = msg_type
    msg.text = text
    msg.start_s = None
    msg.stop_s = None
    msg.turn_idx = None
    msg.interrupted = False
    msg.event = None
    msg.data = None
    msg.tool_call = None
    msg.tool_call_handle = None
    return msg


def _tool_msg(name, args_json="{}"):
    """Create a mock MsgOut for a tool call."""
    tc = MagicMock()
    tc.tool_name = name
    tc.args_json = args_json

    th = AsyncMock()

    msg = MagicMock()
    msg.msg_type = "tool_call"
    msg.tool_call = tc
    msg.tool_call_handle = th
    msg.text = None
    msg.event = None
    msg.data = None
    return msg, th


def test_rejects_non_start_message():
    app = _make_app()
    with fastapi.testclient.TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello"})
            # Server should close with 4000
            try:
                ws.receive_json()
            except Exception:
                pass


@patch("gradbot._gradbot.run")
def test_session_sends_agent_text(mock_run):
    """Verify agent text is forwarded as agent_text."""
    inp = _mock_input_handle()
    out = _mock_output_handle([
        _text_msg("Hello there"),
        None,
    ])
    async def fake_run(**kw):
        return inp, out
    mock_run.side_effect = fake_run

    app = _make_app()
    with fastapi.testclient.TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start"})
            msg = ws.receive_json()
            assert msg["type"] == "agent_text"
            assert msg["text"] == "Hello there"
            ws.send_json({"type": "stop"})


@patch("gradbot._gradbot.run")
def test_session_tool_call_dispatched(mock_run):
    """Verify tool calls are dispatched to on_tool_call."""
    tool_msg, tool_inner = _tool_msg(
        "greet", '{"name": "Alice"}'
    )
    inp = _mock_input_handle()
    out = _mock_output_handle([tool_msg, None])
    async def fake_run(**kw):
        return inp, out
    mock_run.side_effect = fake_run

    received = {}

    async def on_tool(handle, input_handle, websocket):
        received["name"] = handle.name
        received["args"] = handle.args
        await handle.send_json({"greeting": "hi Alice"})

    app = _make_app(on_tool_call=on_tool)
    with fastapi.testclient.TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start"})
            # Give the event loop time to dispatch
            import time
            time.sleep(0.1)
            ws.send_json({"type": "stop"})

    assert received["name"] == "greet"
    assert received["args"] == {"name": "Alice"}
    tool_inner.send.assert_called_once_with(
        '{"greeting": "hi Alice"}'
    )
