"""Voice helpers: JSON export, tool definitions, and tool resolution.

Voices are fetched from the Gradium API on first access and cached.
"""

from __future__ import annotations

import functools
import logging

from gradium import GradiumClient

from . import langs
from . import schemas

logger = logging.getLogger(__name__)


def async_cache(fn):
    """Simple cache decorator for async functions."""
    cache = {}

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        nonlocal cache
        key = ":".join(
            [str(arg) for arg in args]
            + [f"{k}={v}" for k, v in sorted(kwargs.items())]
        )
        if key not in cache:
            result = await fn(*args, **kwargs)
            cache[key] = result
        return cache.get(key)

    def clear():
        nonlocal cache
        cache = {}

    wrapper.cache_clear = clear
    return wrapper


def _tag_value(tags: list[dict], category: str) -> str | None:
    """Extract the first tag value for a given category."""
    return next(
        (t.get("value") for t in tags if t.get("category") == category),
        None,
    )


def _map_api_voice(raw: dict) -> schemas.Voice | None:
    """Convert an API voice response dict to a Voice schema."""
    if not raw.get("name") or not raw.get("uid"):
        return None
    tags = raw.get("tags", [])
    language = raw.get("language") or "en"
    region = (_tag_value(tags, "region") or language).lower()
    gender_tag = (_tag_value(tags, "gender") or "").lower()
    return schemas.Voice(
        name=raw["name"],
        voice_id=raw["uid"],
        language=language,
        country=region,
        country_name=langs.COUNTRY_NAMES.get(region, region.upper()),
        gender=langs.GENDER_NAMES.get(gender_tag, gender_tag.title()),
        description=raw.get("description") or "",
    )


@async_cache
async def load_catalog(base_url: str, api_key: str) -> list[schemas.Voice]:
    """Fetch catalog voices from the Gradium API."""
    try:
        client = GradiumClient(base_url=base_url, api_key=api_key)
        items = await client.voice_get(include_catalog=True)
    except Exception:
        logger.warning("Failed to fetch voice catalog from %s", base_url)
        return []

    return [v for item in items if (v := _map_api_voice(item)) is not None]
