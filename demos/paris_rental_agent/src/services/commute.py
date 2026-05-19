"""Commute time calculation via Google Routes API (computeRouteMatrix).

For each listing we ask Google how long it takes from the user's workplace to
the listing's address, by:

  - WALK      → walk_min
  - BICYCLE   → bike_min
  - TRANSIT   → metro_min  (subway / bus / tram / rail)

Calls are batched: one HTTP request per mode with all destinations. Results
are cached in-memory keyed by (origin, destination, mode), so each unique
trip is paid for exactly once per process.

If no API key is configured we return ``status="unknown"`` for every listing
— scoring still runs and the UI just shows the existing
"Commute needs verification" warning.

The Routes API is the modern replacement for the legacy Distance Matrix API.
Enable "Routes API" in your Google Cloud project for this to work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from ..config import get_settings

logger = logging.getLogger(__name__)

ROUTES_API_URL = (
    "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
)
PARIS_BIAS = "Paris, France"

_TRAVEL_MODE = {
    "transit": "TRANSIT",
    "bicycling": "BICYCLE",
    "walking": "WALK",
}

_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


# ─────────────────────── key resolution ───────────────────────
@lru_cache(maxsize=1)
def _resolve_google_maps_key() -> str:
    """Look in env first, then in config.yaml files (same pattern as Tavily)."""
    settings = get_settings()
    if settings.google_maps_api_key and settings.google_maps_api_key.strip():
        return settings.google_maps_api_key.strip()

    here = Path(__file__).resolve().parents[2]
    candidates = [
        here / "config.yaml",          # demos/paris_rental_agent/config.yaml
        here.parent / "config.yaml",   # demos/config.yaml
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for section_name in ("google_maps", "googlemaps", "google"):
            section = data.get(section_name)
            if isinstance(section, dict):
                key = section.get("api_key") or section.get("key")
                if isinstance(key, str) and key.strip():
                    return key.strip()
    return ""


# ─────────────────────── result helpers ───────────────────────
def _unknown_result(error: str | None = None) -> dict[str, Any]:
    out = {
        "metro_min": None,
        "bike_min": None,
        "walk_min": None,
        "status": "unknown",
    }
    if error:
        out["error"] = error
    return out


# ─────────────────────── Google Routes API call ───────────────────────
def _parse_duration_seconds(s: Any) -> Optional[int]:
    """Routes API returns ``duration`` as a string like '1234s'."""
    if isinstance(s, (int, float)):
        return int(s)
    if isinstance(s, str) and s.endswith("s"):
        try:
            return int(float(s[:-1]))
        except ValueError:
            return None
    return None


async def _call_routes_matrix(
    client: httpx.AsyncClient,
    api_key: str,
    origin: str,
    destinations: list[str],
    mode: str,
) -> list[dict[str, Any]]:
    """One Routes API computeRouteMatrix call. Returns one entry per destination.

    Each item: {"duration_minutes": int|None, "status": str}.
    """
    if not destinations:
        return []

    travel_mode = _TRAVEL_MODE.get(mode)
    if not travel_mode:
        return [
            {"duration_minutes": None, "status": f"bad_mode:{mode}"} for _ in destinations
        ]

    body: dict[str, Any] = {
        "origins": [{"waypoint": {"address": _bias_address(origin)}}],
        "destinations": [
            {"waypoint": {"address": _bias_address(d)}} for d in destinations
        ],
        "travelMode": travel_mode,
    }
    if travel_mode == "TRANSIT":
        # Required for TRANSIT — use a near-future timestamp.
        depart = datetime.now(timezone.utc) + timedelta(minutes=1)
        body["departureTime"] = depart.strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # Field mask is required by Routes API; trim what we ask for.
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition,status",
    }

    try:
        resp = await client.post(
            ROUTES_API_URL, json=body, headers=headers, timeout=20.0
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body_text = exc.response.text[:200] if exc.response is not None else ""
        logger.warning(
            "Google Routes API call failed (mode=%s) HTTP %s: %s",
            mode,
            exc.response.status_code if exc.response else "?",
            body_text,
        )
        return [
            {"duration_minutes": None, "status": f"http_{exc.response.status_code if exc.response else 0}"}
            for _ in destinations
        ]
    except Exception as exc:
        logger.warning("Google Routes API call failed (mode=%s): %s", mode, exc)
        return [
            {"duration_minutes": None, "status": f"error: {exc!s:.80}"}
            for _ in destinations
        ]

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("Google Routes API bad JSON (mode=%s): %s", mode, exc)
        return [{"duration_minutes": None, "status": "bad_json"} for _ in destinations]

    # Response is a JSON array of {originIndex, destinationIndex, duration, condition, status}.
    if not isinstance(data, list):
        msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
        logger.warning("Google Routes API unexpected response (mode=%s): %s", mode, msg or data)
        return [
            {"duration_minutes": None, "status": str(msg or "bad_response")[:80]}
            for _ in destinations
        ]

    # Index by destinationIndex so partial responses still align with input order.
    by_idx: dict[int, dict[str, Any]] = {}
    for item in data:
        if isinstance(item, dict) and "destinationIndex" in item:
            by_idx[item["destinationIndex"]] = item

    out: list[dict[str, Any]] = []
    for i in range(len(destinations)):
        item = by_idx.get(i)
        if not item:
            out.append({"duration_minutes": None, "status": "missing"})
            continue
        condition = item.get("condition")
        if condition != "ROUTE_EXISTS":
            out.append(
                {
                    "duration_minutes": None,
                    "status": condition or item.get("status", {}).get("message", "no_route"),
                }
            )
            continue
        secs = _parse_duration_seconds(item.get("duration"))
        out.append(
            {
                "duration_minutes": int(round(secs / 60)) if secs is not None else None,
                "status": "ok",
            }
        )
    return out


def _bias_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not addr:
        return PARIS_BIAS
    if "paris" in addr.lower() or "france" in addr.lower():
        return addr
    return f"{addr}, {PARIS_BIAS}"


# ─────────────────────── public API ───────────────────────
async def compute_commute_batch(
    origin: str, destinations: list[str]
) -> dict[str, dict[str, Any]]:
    """Compute (metro, bike, walk) commute minutes for many destinations at once.

    Returns a dict keyed by destination string. Always returns one entry per
    input destination even if the call fails — that entry is just
    ``status="unknown"``.
    """
    origin = (origin or "").strip()
    if not origin or not destinations:
        return {d: _unknown_result("missing_origin_or_destinations") for d in destinations}

    api_key = _resolve_google_maps_key()
    if not api_key:
        return {d: _unknown_result("no_google_maps_key") for d in destinations}

    # De-dup destinations so we don't pay twice for the same address.
    unique_destinations = sorted({(d or "").strip() for d in destinations if d and d.strip()})

    # Per-mode list of destinations we still need to fetch.
    to_fetch: dict[str, list[str]] = {"transit": [], "bicycling": [], "walking": []}
    for d in unique_destinations:
        for mode in to_fetch:
            if (origin, d, mode) not in _CACHE:
                to_fetch[mode].append(d)

    if any(to_fetch.values()):
        async with httpx.AsyncClient() as client:
            for mode, dests in to_fetch.items():
                if not dests:
                    continue
                # Routes API: up to 25 origin*destination pairs per call.
                # We use 1 origin, so up to 25 destinations per call.
                for chunk_start in range(0, len(dests), 25):
                    chunk = dests[chunk_start : chunk_start + 25]
                    results = await _call_routes_matrix(
                        client, api_key, origin, chunk, mode
                    )
                    for d, r in zip(chunk, results):
                        _CACHE[(origin, d, mode)] = r

    # Build per-destination output from the cache.
    out: dict[str, dict[str, Any]] = {}
    for d in destinations:
        d_norm = (d or "").strip()
        if not d_norm:
            out[d] = _unknown_result("empty_destination")
            continue
        metro = _CACHE.get((origin, d_norm, "transit"), {}).get("duration_minutes")
        bike = _CACHE.get((origin, d_norm, "bicycling"), {}).get("duration_minutes")
        walk = _CACHE.get((origin, d_norm, "walking"), {}).get("duration_minutes")
        any_known = any(v is not None for v in (metro, bike, walk))
        out[d] = {
            "metro_min": metro,
            "bike_min": bike,
            "walk_min": walk,
            "status": "verified" if any_known else "unknown",
        }
    return out


def is_configured() -> bool:
    return bool(_resolve_google_maps_key())
