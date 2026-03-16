from __future__ import annotations

import pytest
from fastapi import FastAPI


@pytest.fixture()
def app() -> FastAPI:
    """Create a fresh FastAPI app for route tests."""
    return FastAPI()
