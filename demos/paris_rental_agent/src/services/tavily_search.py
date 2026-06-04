"""Tavily integration for supported city rental listings.

The Tavily API key is required. Set it via the ``TAVILY_API_KEY`` env var or
under a ``tavily.api_key`` block in either ``demos/paris_rental_agent/config.yaml``
or ``demos/config.yaml``. If no key is configured, search raises
``TavilyNotConfiguredError``.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import yaml

from ..config import get_settings
from .cities import city_label, normalize_city

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

RENTAL_DOMAINS_BY_CITY = {
    "paris": [
        "pap.fr",
        "seloger.com",
        "bienici.com",
        "leboncoin.fr",
        "jinka.fr",
        "studapart.com",
        "lodgis.com",
        "figaroimmobilier.fr",
    ],
    "berlin": [
        "immobilienscout24.de",
        "immowelt.de",
        "kleinanzeigen.de",
        "wg-gesucht.de",
        "wunderflats.com",
        "housinganywhere.com",
        "spotahome.com",
    ],
}

PARIS_RENTAL_DOMAINS = RENTAL_DOMAINS_BY_CITY["paris"]

BERLIN_RENTAL_DOMAINS = RENTAL_DOMAINS_BY_CITY["berlin"]

ALL_RENTAL_DOMAINS = sorted({domain for domains in RENTAL_DOMAINS_BY_CITY.values() for domain in domains})

TRUSTED_RENTAL_DOMAINS = [
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


def is_obvious_collection_url(url: str) -> bool:
    """Return true for search/category pages that are not individual listings."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    host = parsed.netloc.lower().removeprefix("www.")
    path = unquote(parsed.path.lower())
    if not host or not path:
        return True

    if "pap.fr" in host:
        # PAP detail URLs use /annonces/...-r123456, while search pages often
        # use /annonce/... or /recherche/...
        return not (path.startswith("/annonces/") and re.search(r"-r\d+\b", path))
    if "seloger.com" in host:
        return not path.startswith("/annonces/")
    if "bienici.com" in host:
        return not path.startswith("/annonce/")
    if "leboncoin.fr" in host:
        return not (path.startswith("/ad/") or "/ad/" in path)
    if "lodgis.com" in host:
        return path.endswith(".cat.html") or not path.endswith(".html")
    if "figaroimmobilier.fr" in host:
        return "/annonces/" not in path and "/locations/" not in path
    if "studapart.com" in host:
        return not re.search(r"/(?:fr|en)/(?:logement|accommodation)/", path)
    if "jinka.fr" in host:
        return path in {"", "/"} or "/recherche" in path
    if "immobilienscout24.de" in host:
        return not re.search(r"/expose/\d+", path)
    if "immowelt.de" in host:
        return not re.search(r"/expose/[a-z0-9]+", path)
    if "kleinanzeigen.de" in host:
        return not path.startswith("/s-anzeige/")
    if "wg-gesucht.de" in host:
        return not re.search(r"\.\d+\.\d+\.\d+\.\d+\.html$", path)
    if "wunderflats.com" in host:
        return not path.startswith("/en/furnished-apartment/")
    if "housinganywhere.com" in host:
        return not re.search(r"/room/\d+", path)
    if "spotahome.com" in host:
        return not re.search(r"/berlin/for-rent:apartments/\d+", path)
    return False


def build_queries(profile: dict[str, Any]) -> list[str]:
    """Generate a small set of focused queries from a search profile."""
    city = normalize_city(profile.get("city"))
    city_name = city_label(city)
    rooms_label = "studio"
    if profile.get("min_bedrooms"):
        n = profile["min_bedrooms"]
        if city == "berlin":
            rooms_label = "1 Zimmer Wohnung" if n == 1 else f"{n} Zimmer Wohnung"
        else:
            rooms_label = "1 chambre" if n == 1 else f"{n} chambres"
    elif profile.get("min_rooms"):
        n = profile["min_rooms"]
        if city == "berlin":
            rooms_label = "Studio" if n == 1 else f"{n} Zimmer Wohnung"
        else:
            rooms_label = "studio" if n == 1 else f"{n} pièces"

    furnished = ""
    if profile.get("furnished_preference") == "required":
        furnished = "möbliert" if city == "berlin" else "meublé"

    budget_str = ""
    if profile.get("max_rent_including_charges_eur"):
        budget_str = f"{profile['max_rent_including_charges_eur']} euros"

    arrondissements = profile.get("preferred_arrondissements") or []
    arr_str = ""
    if city == "paris" and arrondissements:
        arr_str = " ".join(f"Paris {a}e" for a in arrondissements)

    queries = []
    if city == "berlin":
        base = f"Wohnung mieten Berlin {rooms_label}".strip()
        if furnished:
            queries.append(f"{base} {furnished} {budget_str}".strip())
        queries.append(f"{base} Warmmiete {budget_str}".strip())
        queries.append(f"site:immobilienscout24.de/expose Wohnung Berlin {rooms_label} {budget_str}".strip())
        queries.append(f"site:immowelt.de/expose Berlin Wohnung mieten {rooms_label} {budget_str}".strip())
        queries.append(f"site:wunderflats.com/en/furnished-apartment Berlin {rooms_label} {budget_str}".strip())
        queries.append(f"site:spotahome.com/berlin/for-rent:apartments Berlin {rooms_label} {budget_str}".strip())
    else:
        base = f"location appartement Paris {rooms_label}".strip()
        if furnished:
            queries.append(f"{base} {furnished} {budget_str}".strip())
        queries.append(f"{base} charges comprises {budget_str}".strip())
        queries.append(f"site:pap.fr/annonces appartement Paris {rooms_label} {budget_str}".strip())
        queries.append(f"site:seloger.com/annonces/locations/appartement Paris {rooms_label} {budget_str}".strip())
        queries.append(f"site:bienici.com/annonce/location Paris appartement {furnished}".strip())
        if arr_str:
            queries.append(f"location appartement {arr_str} {rooms_label} {furnished}".strip())

    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        norm = " ".join(q.split())
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out[:6]


async def _tavily_request(
    client: httpx.AsyncClient, api_key: str, query: str, *, city: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Call Tavily for one query. Returns (results, error_message)."""
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": True,
        "include_domains": RENTAL_DOMAINS_BY_CITY.get(city, PARIS_RENTAL_DOMAINS),
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


async def search_city_rentals(profile: dict[str, Any]) -> list[dict[str, Any]]:
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

    city = normalize_city(profile.get("city"))
    queries = build_queries(profile)
    if not queries:
        return []

    raw_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    errors: list[str] = []

    async with httpx.AsyncClient() as client:
        for q in queries:
            results, err = await _tavily_request(client, api_key, q, city=city)
            if err:
                errors.append(err)
            for r in results:
                url = (r.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                if is_obvious_collection_url(url):
                    logger.info("Skipping non-listing collection URL from Tavily: %s", url)
                    continue
                seen_urls.add(url)
                raw_results.append(
                    {
                        "url": url,
                        "title": r.get("title") or "Untitled",
                        "content": r.get("content") or r.get("raw_content") or "",
                        "source": _domain_of(url),
                        "is_mock": False,
                    }
                )

    if not raw_results and errors:
        raise TavilySearchError("; ".join(errors[:3]))
    return raw_results


async def search_paris_rentals(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible alias for tests and older imports."""
    return await search_city_rentals({**profile, "city": "paris"})


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        netloc = urlparse(url).netloc
        return netloc.replace("www.", "")
    except Exception:
        return ""
