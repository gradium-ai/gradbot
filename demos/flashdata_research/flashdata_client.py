"""Authenticated FlashData MCP access for the research example, without retries."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ENDPOINT = "https://data.flashdata.dev/mcp"
QUERY_TOOLS = frozenset(
    {
        "google_search",
        "google_news",
        "google_images",
        "google_shopping",
        "google_videos",
        "google_places",
        "youtube_search",
        "youtube_search_extended",
        "youtube_metadata",
        "youtube_transcript",
        "youtube_captions",
        "youtube_trainability",
        "youtube_channel",
        "youtube_suggestions",
    }
)
ALLOWED_TOOLS = QUERY_TOOLS | {"get_pricing", "get_job", "get_result"}
FAILURE = (
    "FlashData request did not return a usable result. It may already have consumed Credits. "
    "Do not repeat the submission. Check existing Jobs in your FlashData console first."
)


class FlashDataClient:
    """Keep API credentials outside tool arguments and restrict the voice tool surface."""

    def __init__(
        self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._api_key = api_key
        self._transport = transport

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        async with httpx.AsyncClient(
            headers={"X-API-Key": self._api_key},
            timeout=120,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as http:
            async with streamable_http_client(ENDPOINT, http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def catalog(self) -> dict[str, Any]:
        """Discover current schemas for the user's permitted query tools and supporting reads."""
        try:
            async with self._session() as session:
                result = await session.list_tools()
                return {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "input_schema": t.inputSchema,
                        }
                        for t in result.tools
                        if t.name in ALLOWED_TOOLS
                    ]
                }
        except Exception:
            return {
                "error": "Could not discover FlashData tools. Check your key and source permissions."
            }

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Submit once; never expose download storage inputs through the voice session."""
        if name not in ALLOWED_TOOLS:
            return {
                "error": "Tool unavailable in this voice example. Use the protected download CLI."
            }
        try:
            async with self._session() as session:
                result = await session.call_tool(
                    name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=120),
                )
                if result.isError:
                    return {"error": FAILURE}
                body = result.structuredContent
                if body is None:
                    body = json.loads(
                        next(
                            item.text for item in result.content if item.type == "text"
                        )
                    )
                if not isinstance(body, dict):
                    return {"error": FAILURE}
                if (
                    name == "get_job"
                    and body.get("job", {}).get("source") == "youtube_download"
                ):
                    job = body["job"]
                    return {
                        "job": {"id": job.get("id"), "status": job.get("status")},
                        "message": "Inspect the private delivery manifest in your protected local CLI.",
                    }
                return body
        except Exception:
            # Transport exceptions may include response bodies or URLs. Keep them out of voice history.
            return {"error": FAILURE}
