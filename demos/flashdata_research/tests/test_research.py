import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import gradbot
import httpx
import pytest
from fastapi.testclient import TestClient

from flashdata_agent import TOOLS, ResearchState, make_config, on_tool_call
from flashdata_client import (
    ALLOWED_TOOLS,
    ENDPOINT,
    FAILURE,
    QUERY_TOOLS,
    FlashDataClient,
)

REQUESTS = json.loads(Path(__file__).with_name("requests.json").read_text())


class MCPFixture:
    def __init__(self, *, failure: str | None = None, text_result: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.failure = failure
        self.text_result = text_result

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ENDPOINT
        assert request.headers["X-API-Key"] == "test-only-key"
        if request.method != "POST":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            names = ALLOWED_TOOLS | {"youtube_download", "submit_batch", "future_write"}
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Fixture tool",
                        "inputSchema": {"type": "object"},
                    }
                    for name in sorted(names)
                ]
            }
        else:
            assert method == "tools/call"
            self.calls.append(payload["params"])
            if self.failure == "redirect":
                return httpx.Response(
                    307, headers={"Location": "https://untrusted.invalid/mcp"}
                )
            if self.failure == "timeout":
                raise httpx.ReadTimeout(
                    "private-url-and-storage-secret", request=request
                )
            if self.failure == "tool":
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "private-secret"}],
                }
            else:
                name = payload["params"]["name"]
                body = {
                    "source": name,
                    "results": [
                        {"title": "Fixture only", "url": "https://example.com"}
                    ],
                }
                if name == "get_job":
                    body = {
                        "job": {
                            "id": "job_fixture",
                            "source": "youtube_download",
                            "status": "completed",
                        },
                        "output": {"signed_url": "private-url-and-storage-secret"},
                    }
                if name == "get_result":
                    body = {
                        "result_id": "result_fixture",
                        "next_offset": 12,
                        "chunk": "fixture-part",
                    }
                result = {"content": [{"type": "text", "text": json.dumps(body)}]}
                if not self.text_result:
                    result["structuredContent"] = body
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}
        )

    def client(self):
        return FlashDataClient(
            "test-only-key", transport=httpx.MockTransport(self.handle)
        )


class Handle:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}
        self.result = None

    async def send_json(self, result):
        self.result = result

    async def send_error(self, error):
        self.result = {"error": error}


async def test_catalog_binding_and_storage_filter():
    fixture = MCPFixture()
    handle = Handle("flashdata_catalog")
    await on_tool_call(ResearchState(fixture.client()), handle, None, None)
    assert {t["name"] for t in handle.result["tools"]} == ALLOWED_TOOLS
    assert not fixture.calls


@pytest.mark.parametrize("name", sorted(QUERY_TOOLS))
async def test_gradbot_callback_submits_one_request(name):
    fixture = MCPFixture()
    handle = Handle(
        "flashdata_call", {"name": name, "arguments_json": json.dumps(REQUESTS[name])}
    )
    await on_tool_call(ResearchState(fixture.client()), handle, None, None)
    assert handle.result["source"] == name
    assert fixture.calls == [{"name": name, "arguments": REQUESTS[name]}]


@pytest.mark.parametrize("failure", ["redirect", "timeout", "tool"])
async def test_paid_errors_not_replayed_or_disclosed(failure):
    fixture = MCPFixture(failure=failure)
    result = await fixture.client().call("google_search", {"query": "fixture"})
    assert result == {"error": FAILURE} and len(fixture.calls) == 1


@pytest.mark.parametrize("name", ["youtube_download", "submit_batch", "future_write"])
async def test_blocked_tools(name):
    fixture = MCPFixture()
    assert "error" in await fixture.client().call(name, {})
    assert not fixture.calls


@pytest.mark.parametrize("arguments", ["[]", "not-json", None])
async def test_bad_arguments_do_not_submit(arguments):
    fixture = MCPFixture()
    handle = Handle(
        "flashdata_call", {"name": "google_search", "arguments_json": arguments}
    )
    await on_tool_call(ResearchState(fixture.client()), handle, None, None)
    assert "error" in handle.result and not fixture.calls


async def test_private_delivery_and_chunk_metadata():
    client = MCPFixture(text_result=True).client()
    result = await client.call("get_job", {"job_id": "job_fixture"})
    assert "output" not in result and "storage-secret" not in json.dumps(result)
    result = await client.call("get_result", {"result_id": "result_fixture"})
    assert result["next_offset"] == 12


def test_real_native_session_config():
    cfg = gradbot.config.Config()
    config = make_config(ResearchState(MCPFixture().client()), cfg, {"language": "en"})
    assert isinstance(config, gradbot.SessionConfig)
    assert len(TOOLS) == 2


def test_static_frontend_routes_without_paid_calls():
    from main import app

    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "FlashData Credits" in page.text
        for path in [
            "/static/logo.svg",
            "/static/js/opus-encoder.js",
            "/static/js/audio-processor.js",
            "/static/js/synced-audio-player.js",
            "/api/audio-config",
        ]:
            assert client.get(path).status_code == 200
        assert client.get("/api/voices").status_code == 404


async def test_unconfigured_websocket_rejects_before_voice(monkeypatch):
    import main

    monkeypatch.delenv("FLASHDATA_API_KEY", raising=False)
    closes = []

    async def close(**kwargs):
        closes.append(kwargs)

    await main.ws_chat(SimpleNamespace(close=close))
    assert closes[0]["code"] == 1008
