"""Tiny async client for the Keenable Search API (realtime mode).

API reference: https://docs.keenable.ai/api-reference/search
    POST https://api.keenable.ai/v1/search          (with X-API-Key: keen_...)
    POST https://api.keenable.ai/v1/search/public   (no key, X-Keenable-Title)
    Body:   {"query": "...", "mode": "realtime"}
    Returns: {"results": [{"title", "url", "description", "snippet"}]}

Realtime mode is the fastest Keenable search mode — ideal for a low-latency
voice agent. ``KEENABLE_API_KEY`` is optional: without it the demo uses the
keyless public endpoint, which is rate-limited per IP; a key raises the limits.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

KEENABLE_API_URL = "https://api.keenable.ai/v1/search"
KEENABLE_PUBLIC_URL = "https://api.keenable.ai/v1/search/public"
# The keyless endpoint requires an app name; it identifies the demo, not the user.
KEENABLE_APP_TITLE = "gradbot"


class KeenableSearchError(RuntimeError):
    """Raised when the Keenable search request fails."""


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


async def keenable_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Run a Keenable realtime web search and return normalized result rows.

    Each row: ``{"title", "url", "source", "snippet"}``.

    Raises:
        KeenableSearchError: if the request fails (auth, rate limit, network…).
    """
    api_key = (os.environ.get("KEENABLE_API_KEY") or "").strip()
    payload = {"query": query, "mode": "realtime"}
    if api_key:
        url = KEENABLE_API_URL
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    else:
        url = KEENABLE_PUBLIC_URL
        headers = {"X-Keenable-Title": KEENABLE_APP_TITLE, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers, timeout=30.0)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response is not None else ""
        msg = f"Keenable HTTP {e.response.status_code}: {body}"
        if e.response.status_code == 429 and not api_key:
            msg += " (keyless rate limit; set KEENABLE_API_KEY for higher limits)"
        logger.warning("Keenable search failed for %r: %s", query, msg)
        raise KeenableSearchError(msg) from e
    except Exception as e:
        logger.warning("Keenable search failed for %r: %s", query, e)
        raise KeenableSearchError(str(e)) from e

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (data.get("results") or []):
        url = (item.get("url") or item.get("uri") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append(
            {
                "title": item.get("title") or "Untitled",
                "url": url,
                "source": _domain_of(url),
                # Prefer the richer snippet; fall back to the short description.
                "snippet": (item.get("snippet") or item.get("description") or "").strip(),
            }
        )
        if len(rows) >= limit:
            break
    return rows
