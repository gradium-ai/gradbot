"""Test fixtures: in-memory SQLite, FastAPI test client, fake Tavily."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Use a temp SQLite db for tests, before any app modules are imported
_db_file = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_db_file.name}"
os.environ["SECRET_KEY"] = "test-secret-0123456789abcdef012345"
os.environ["TAVILY_API_KEY"] = ""

# Use the SAME import path as main.py: add paris_rental_agent/ to sys.path so
# `app.*` resolves identically. Without this, conftest would import the
# package under `demos.paris_rental_agent.app.*` while the running FastAPI app
# would import it under `app.*` — two distinct module objects, breaking
# monkeypatching.
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


@pytest.fixture(scope="session")
def app():
    """FastAPI app (lazy import after env vars are set)."""
    from app.db import init_db

    init_db()
    from main import app as fastapi_app

    return fastapi_app


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def db_session():
    from app.db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture(autouse=True)
def fake_tavily(monkeypatch):
    """Replace the Tavily HTTP call with a fixed set of normalized listings.

    Production now requires a real Tavily key. Tests run offline by default,
    so we patch ``search_paris_rentals`` to return a deterministic set of raw
    Paris rental results.
    """
    from app.services import search_pipeline

    fake_results = [
        {
            "url": "https://example.test/listing/strong-match",
            "title": "Lumineux 2 pièces meublé Paris 11ème",
            "content": (
                "Bel appartement meublé de 38 m², 1 chambre, cuisine équipée "
                "(four, lave-vaisselle), beaucoup de lumière naturelle, balcon, métro "
                "République à 5 min. Loyer 1 450 € charges comprises. Paris 11ème (75011)."
            ),
            "source": "pap.fr",
            "is_mock": False,
        },
        {
            "url": "https://example.test/listing/over-budget",
            "title": "2 pièces meublé Saint-Germain",
            "content": (
                "Appartement meublé de 45 m², 1 chambre, cuisine équipée, lumineux, "
                "Paris 6ème (75006). Loyer 2 100 € charges comprises."
            ),
            "source": "seloger.com",
            "is_mock": False,
        },
        {
            "url": "https://example.test/listing/too-small",
            "title": "Petit studio Paris 11",
            "content": (
                "Studio meublé de 14 m², lit double, cuisine équipée, "
                "Paris 11ème (75011). Loyer 950 € charges comprises."
            ),
            "source": "pap.fr",
            "is_mock": False,
        },
        {
            "url": "https://example.test/listing/unfurnished",
            "title": "2 pièces non meublé près Bastille",
            "content": (
                "Appartement non meublé de 42 m², 1 chambre, cuisine équipée, ascenseur, "
                "métro Bastille à 4 min. Paris 11ème (75011). Loyer 1 350 € + charges 80 €."
            ),
            "source": "bienici.com",
            "is_mock": False,
        },
    ]

    async def _fake(profile):
        return fake_results

    monkeypatch.setattr(search_pipeline, "search_paris_rentals", _fake)

    # Default commute mock: every destination returns "unknown".
    # Individual tests can override this when they exercise the commute path.
    async def _fake_commute(origin, destinations):
        return {
            d: {
                "metro_min": None,
                "bike_min": None,
                "walk_min": None,
                "status": "unknown",
            }
            for d in destinations
        }

    monkeypatch.setattr(search_pipeline, "compute_commute_batch", _fake_commute)
