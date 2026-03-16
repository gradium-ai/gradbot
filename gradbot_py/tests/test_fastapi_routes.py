"""Tests for setup_demo_routes FastAPI integration."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gradbot.fastapi import setup_demo_routes


class TestAudioConfig:
    @pytest.mark.parametrize("use_pcm", [False, True])
    def test_pcm_config(self, app: FastAPI, use_pcm: bool):
        setup_demo_routes(app, use_pcm=use_pcm)
        resp = TestClient(app).get("/api/audio-config")
        assert resp.status_code == 200
        assert resp.json() == {"pcm": use_pcm}


class TestVoicesRoute:
    def test_not_registered_by_default(self, app: FastAPI):
        setup_demo_routes(app)
        resp = TestClient(app).get("/api/voices")
        assert resp.status_code == 404

    def test_registered_when_enabled(self, app: FastAPI):
        setup_demo_routes(app, voices=True)
        resp = TestClient(app).get("/api/voices")
        assert resp.status_code == 200
        data = resp.json()
        assert "voices" in data
        assert isinstance(data["voices"], list)
        assert len(data["voices"]) > 0


class TestStaticRoutes:
    def test_index_served_when_exists(self, app: FastAPI):
        with tempfile.TemporaryDirectory() as tmpdir:
            index = Path(tmpdir) / "index.html"
            index.write_text("<h1>hello</h1>")
            setup_demo_routes(app, static_dir=tmpdir)
            resp = TestClient(app).get("/")
            assert resp.status_code == 200
            assert "<h1>hello</h1>" in resp.text

    def test_index_404_when_missing(self, app: FastAPI):
        with tempfile.TemporaryDirectory() as tmpdir:
            setup_demo_routes(app, static_dir=tmpdir)
            resp = TestClient(app).get("/")
            assert resp.status_code == 404

    def test_static_mount_serves_files(self, app: FastAPI):
        with tempfile.TemporaryDirectory() as tmpdir:
            css = Path(tmpdir) / "style.css"
            css.write_text("body { color: red; }")
            setup_demo_routes(app, static_dir=tmpdir)
            resp = TestClient(app).get("/static/style.css")
            assert resp.status_code == 200
            assert "color: red" in resp.text
