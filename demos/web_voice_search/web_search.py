"""Explicit search provider selection for the voice web search demo."""

import asyncio
import json
import os
from urllib.parse import urlparse

import httpx
from keenable_search import keenable_search
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "gradbot-web-voice-search/0.1.0 (+https://github.com/gradium-ai/gradbot)"


class ParallelSearchError(RuntimeError):
    """Parallel could not complete the search."""


def selected_provider() -> str:
    """Keep Keenable as the default; never silently replace an explicit choice."""
    provider = os.environ.get("WEB_SEARCH_PROVIDER", "keenable").strip().lower()
    if provider not in {"keenable", "parallel"}:
        raise ValueError("WEB_SEARCH_PROVIDER must be keenable or parallel")
    return provider


async def search(query: str, *, provider: str, session_id: str, limit: int = 5):
    """Return the existing title/url/source/snippet rows for the chosen provider."""
    if provider == "keenable":
        return await keenable_search(query, limit=limit)
    if provider != "parallel":
        raise ValueError("Unknown web search provider")

    try:
        # Bound the entire MCP lifecycle, including discovery and teardown.
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
            ) as client:
                async with streamable_http_client(
                    PARALLEL_MCP_URL, http_client=client
                ) as (read, write, _):
                    async with ClientSession(
                        read,
                        write,
                        client_info=Implementation(
                            name="gradbot-web-voice-search", version="0.1.0"
                        ),
                    ) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        if not any(tool.name == "web_search" for tool in tools.tools):
                            raise ParallelSearchError("Parallel has no web_search tool")
                        result = await session.call_tool(
                            "web_search",
                            {
                                "objective": query,
                                "search_queries": [query],
                                "session_id": session_id,
                            },
                        )
                        if result.isError:
                            raise ParallelSearchError("Parallel returned a tool error")
                        data = result.structuredContent
                        if data is None:
                            data = json.loads(
                                next(
                                    item.text
                                    for item in result.content
                                    if item.type == "text"
                                )
                            )
        rows = []
        seen = set()
        for item in data["results"]:
            url = item["url"].strip()
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append(
                {
                    "title": item.get("title") or "Untitled",
                    "url": url,
                    "source": urlparse(url).netloc.removeprefix("www."),
                    "snippet": "\n".join(item["excerpts"]).strip(),
                }
            )
            if len(rows) >= limit:
                break
        return rows
    except ParallelSearchError:
        raise
    except Exception as exc:
        raise ParallelSearchError("Parallel web search failed") from exc
