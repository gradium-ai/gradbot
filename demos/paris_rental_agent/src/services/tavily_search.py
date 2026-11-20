"""Tavily integration for Paris rental listings.

The Tavily API key is required. Set it via the ``TAVILY_API_KEY`` env var or
under a ``tavily.api_key`` block in either ``demos/paris_rental_agent/config.yaml``
or ``demos/config.yaml``. If no key is configured, search raises
``TavilyNotConfiguredError``.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import get_settings

logger = logging.getLogger(__name__)


class TavilyNotConfiguredError(RuntimeError):
    """Raised when the Tavily API key is missing."""


class TavilySearchError(RuntimeError):
    """Raised when Tavily refuses every query (auth, rate limit, network, …)."""


@lru_cache(maxsize=1)
def _resolve_tavily_key() -> str:
    """Look for the Tavily API key in env first, then in config.yaml files.

    gradbot's Config silently drops unknown YAML keys, so a `tavily.api_key`
    entry in config.yaml is invisible to the gradbot loader. We read those
    YAML files directly so users can keep all their secrets in one place.
    """
    settings = get_settings()
    if settings.tavily_api_key and settings.tavily_api_key.strip():
        return settings.tavily_api_key.strip()

    here = Path(__file__).resolve().parents[2]
    candidates = [
        here / "config.yaml",            # demos/paris_rental_agent/config.yaml
        here.parent / "config.yaml",     # demos/config.yaml (shared)
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        section = data.get("tavily") if isinstance(data, dict) else None
        if isinstance(section, dict):
            key = section.get("api_key") or section.get("key")
            if isinstance(key, str) and key.strip():
                return key.strip()
    return ""

PARIS_RENTAL_DOMAINS = [
    "pap.fr",
    "seloger.com",
    "bienici.com",
    "leboncoin.fr",
    "jinka.fr",
    "studapart.com",
    "lodgis.com",
    "figaroimmobilier.fr",
]

TAVILY_API_URL = "https://api.tavily.com/search"


def build_queries(profile: dict[str, Any]) -> list[str]:
    """Generate a small set of focused queries from a search profile."""
    rooms_label = "studio"
    if profile.get("min_bedrooms"):
        n = profile["min_bedrooms"]
        rooms_label = "1 chambre" if n == 1 else f"{n} chambres"
    elif profile.get("min_rooms"):
        n = profile["min_rooms"]
        rooms_label = "studio" if n == 1 else f"{n} pièces"

    furnished = ""
    if profile.get("furnished_preference") == "required":
        furnished = "meublé"

    budget_str = ""
    if profile.get("max_rent_including_charges_eur"):
        budget_str = f"{profile['max_rent_including_charges_eur']} euros"

    arrondissements = profile.get("preferred_arrondissements") or []
    arr_str = ""
    if arrondissements:
        arr_str = " ".join(f"Paris {a}e" for a in arrondissements)

    queries = []
    base = f"location appartement Paris {rooms_label}".strip()
    if furnished:
        queries.append(f"{base} {furnished} {budget_str}".strip())
    queries.append(f"{base} charges comprises {budget_str}".strip())
    queries.append(f"PAP location appartement Paris {rooms_label}".strip())
    queries.append(f"Bienici location appartement Paris {furnished}".strip())
    queries.append(f"location appartement Paris proche métro {rooms_label}".strip())
    if arr_str:
        queries.append(f"location appartement {arr_str} {rooms_label} {furnished}".strip())

    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        norm = " ".join(q.split())
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out[:5]


async def _tavily_request(
    client: httpx.AsyncClient, api_key: str, query: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Call Tavily for one query. Returns (results, error_message)."""
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_domains": PARIS_RENTAL_DOMAINS,
    }
    try:
        r = await client.post(TAVILY_API_URL, json=payload, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        return data.get("results", []) or [], None
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response is not None else ""
        msg = f"Tavily HTTP {e.response.status_code}: {body}"
        logger.warning("Tavily query failed for %r: %s", query, msg)
        return [], msg
    except Exception as e:
        logger.warning("Tavily query failed for %r: %s", query, e)
        return [], str(e)


async def search_paris_rentals(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Run Tavily search and return raw results.

    Raises:
        TavilyNotConfiguredError: if no API key is set.
        TavilySearchError: if every query failed (auth, rate limit, network…).
    """
    api_key = _resolve_tavily_key()
    if not api_key:
        raise TavilyNotConfiguredError(
            "Tavily API key is not configured. Add `tavily.api_key: tvly-...` to "
            "demos/paris_rental_agent/config.yaml or set the TAVILY_API_KEY env var."
        )

    queries = build_queries(profile)
    if not queries:
        return []

    raw_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    errors: list[str] = []

    async with httpx.AsyncClient() as client:
        responses = await asyncio.gather(
            *(_tavily_request(client, api_key, q) for q in queries)
        )

    for results, err in responses:
        if err:
            errors.append(err)
        for r in results:
            url = (r.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            raw_results.append(
                {
                    "url": url,
                    "title": r.get("title") or "Untitled",
                    "content": r.get("content") or "",
                    "source": _domain_of(url),
                    "is_mock": False,
                }
            )

    if not raw_results and errors:
        raise TavilySearchError("; ".join(errors[:3]))
    return raw_results


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        netloc = urlparse(url).netloc
        return netloc.replace("www.", "")
    except Exception:
        return ""
