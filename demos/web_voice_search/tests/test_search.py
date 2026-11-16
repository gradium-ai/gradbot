import asyncio
import json
import os

import fastapi.testclient
import pytest
import web_search
from keenable_search import KeenableNotConfiguredError


def test_default_and_explicit_provider(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    assert web_search.selected_provider() == "keenable"
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "keenable")
    assert web_search.selected_provider() == "keenable"
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "parallel")
    assert web_search.selected_provider() == "parallel"
    for value in ("", "unknown"):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", value)
        with pytest.raises(ValueError):
            web_search.selected_provider()


async def test_missing_keenable_key_does_not_fallback(monkeypatch):
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    with pytest.raises(KeenableNotConfiguredError):
        await web_search.search("Paris", provider="keenable", session_id="unused")


async def test_mcp_transport_and_normalization(transport_server, monkeypatch):
    url, seen = transport_server
    monkeypatch.setattr(web_search, "PARALLEL_MCP_URL", url + "/mcp/")
    result = await web_search.search(
        "Paris", provider="parallel", session_id="chat-1", limit=1
    )
    assert result == [
        {
            "title": "France",
            "url": "https://www.example.org/france",
            "source": "example.org",
            "snippet": "Paris is the capital of France.",
        }
    ]
    assert seen["arguments"]["session_id"] == "chat-1"
    methods = {r["method"] for r in seen["requests"]}
    assert {"initialize", "tools/list", "tools/call"} <= methods
    assert all(
        r["user_agent"] == web_search.USER_AGENT and r["authorization"] is None
        for r in seen["requests"]
    )
    result = await web_search.search("Paris", provider="parallel", session_id="chat-1")
    assert len(result) == 2 and result[1]["title"] == "Untitled"
    assert (
        await web_search.search("empty", provider="parallel", session_id="chat-1") == []
    )


async def test_mcp_error_and_cancellation(transport_server, monkeypatch):
    url, _ = transport_server
    monkeypatch.setattr(web_search, "PARALLEL_MCP_URL", url + "/mcp/")
    with pytest.raises(web_search.ParallelSearchError):
        await web_search.search("error", provider="parallel", session_id="chat-1")
    task = asyncio.create_task(
        web_search.search("slow", provider="parallel", session_id="chat-1")
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_native_demo_round_trip(demo, transport_server, monkeypatch):
    """Use the real Rust remote client and tool handle, with a protocol fixture."""
    url, seen = transport_server
    live = os.environ.get("WEB_SEARCH_LIVE_TEST") == "1"
    if not live:
        monkeypatch.setattr(web_search, "PARALLEL_MCP_URL", url + "/mcp/")
    else:
        real_client = web_search.httpx.AsyncClient

        async def observe(request):
            if request.method == "POST":
                body = json.loads(request.content)
                seen["requests"].append(
                    {
                        "method": body.get("method"),
                        "params": body.get("params"),
                        "user_agent": request.headers.get("user-agent"),
                        "authorization": request.headers.get("authorization"),
                    }
                )

        def traced_client(**kwargs):
            return real_client(**kwargs, event_hooks={"request": [observe]})

        monkeypatch.setattr(web_search.httpx, "AsyncClient", traced_client)
    demo.cfg.gradbot_server.url = url.replace("http://", "ws://") + "/backend"
    with fastapi.testclient.TestClient(demo.app) as client:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "start"})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "agent_text":
                    assert "Paris" in event["text"] and "France" in event["text"]
                    ws.send_json({"type": "stop"})
                    break
    config = seen["config"]["config"]
    assert config["tools"][0]["name"] == "web_search"
    assert "web_search" in config["instructions"]
    result = seen["tool_result"]
    assert result["type"] == "tool_call.result" and result["is_error"] is False
    assert result["result"]["success"] is True
    cards = next(e for e in events if e["type"] == "search_results")
    assert cards["results"][0]["source"]
    if not live:
        assert cards["results"][0]["source"] == "example.org"
        assert len(seen["arguments"]["session_id"]) == 32
    assert all(
        r["user_agent"] == web_search.USER_AGENT and r["authorization"] is None
        for r in seen["requests"]
    )
    print(
        "ROUND_TRIP_EVIDENCE="
        + json.dumps(
            {
                "live": live,
                "config": seen["config"],
                "requests": seen["requests"],
                "tool_result": result,
                "events": events,
            }
        )
    )


async def test_total_search_timeout(transport_server, monkeypatch):
    url, _ = transport_server
    monkeypatch.setattr(web_search, "PARALLEL_MCP_URL", url + "/mcp/")
    real_timeout = asyncio.timeout
    monkeypatch.setattr(web_search.asyncio, "timeout", lambda _: real_timeout(0.2))
    with pytest.raises(web_search.ParallelSearchError):
        await web_search.search("slow", provider="parallel", session_id="chat-timeout")


def test_demo_loader_preserves_default(demo, monkeypatch):
    import importlib

    assert demo.SEARCH_PROVIDER == "parallel"
    monkeypatch.delenv("WEB_SEARCH_PROVIDER")
    assert importlib.reload(demo).SEARCH_PROVIDER == "keenable"
