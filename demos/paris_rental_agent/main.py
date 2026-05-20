"""FastAPI app entry-point for the Paris rental apartment hunting agent.

Run locally either way:
    # from the repo root (Render-style):
    uvicorn demos.paris_rental_agent.main:app --reload --port 8000

    # or from this directory:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure absolute `app.*` imports resolve whether this module is loaded as
# `main` (uvicorn invoked from this directory) or `demos.paris_rental_agent.main`
# (Render / repo-root invocation).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import gradbot  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.db import init_db  # noqa: E402
from src.services.commute import is_configured as is_google_maps_configured  # noqa: E402
from src.services.tavily_search import _resolve_tavily_key  # noqa: E402
from src.routes.assistant import router as assistant_router  # noqa: E402
from src.routes.auth import router as auth_router  # noqa: E402
from src.routes.intake import router as intake_router  # noqa: E402
from src.routes.listings import router as listings_router  # noqa: E402
from src.routes.profiles import router as profiles_router  # noqa: E402
from src.routes.search import router as search_router  # noqa: E402
from src.routes.voice import router as voice_router  # noqa: E402

logger = logging.getLogger(__name__)
gradbot.init_logging()

settings = get_settings()


def _safe_database_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<configured>"


def _startup() -> None:
    settings.validate_runtime_security()
    init_db()
    logger.info("DB initialized at %s", _safe_database_url(settings.database_url))
    if not _resolve_tavily_key():
        logger.warning(
            "Tavily API key is not set — /api/search-runs will return HTTP 503 "
            "until you add `tavily.api_key` to config.yaml or set TAVILY_API_KEY."
        )
    else:
        logger.info("Tavily API key loaded — live search enabled.")
    if not is_google_maps_configured():
        logger.warning(
            "Google Maps API key is not set — commute estimates will stay "
            "unverified until GOOGLE_MAPS_API_KEY is configured."
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    yield


app = FastAPI(title="Paris Rental Agent", lifespan=_lifespan)


# REST routes
app.include_router(auth_router)
app.include_router(intake_router)
app.include_router(profiles_router)
app.include_router(search_router)
app.include_router(listings_router)
app.include_router(assistant_router)

# Voice WebSocket route (handles its own auth)
app.include_router(voice_router)


# Static frontend served by gradbot.routes (also exposes /static/js/*)
_STATIC_DIR = Path(__file__).parent / "static"
_VOICE_CONFIG = gradbot.config.load(Path(__file__).parent)
gradbot.routes.setup(
    app,
    config=_VOICE_CONFIG,
    static_dir=_STATIC_DIR,
)


@app.get("/")
def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/onboarding")
def onboarding_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "env": settings.app_env,
        "tavily_configured": bool(_resolve_tavily_key()),
        "google_maps_configured": is_google_maps_configured(),
    }
