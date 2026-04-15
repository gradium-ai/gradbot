"""Tests for gradbot.routes.setup."""

import tempfile
import pathlib

import fastapi
import fastapi.testclient
import pydantic
import pytest

import gradbot
from gradbot.config import Config, GradiumConfig


@pytest.fixture()
def app():
    return fastapi.FastAPI()


@pytest.mark.parametrize("use_pcm", [False, True])
def test_audio_config(app, use_pcm):
    cfg = Config(use_pcm=use_pcm)
    gradbot.routes.setup(app, config=cfg)
    client = fastapi.testclient.TestClient(app)
    resp = client.get("/api/audio-config")
    assert resp.json() == {"pcm": use_pcm}


def test_voices_not_registered_by_default(app):
    gradbot.routes.setup(app)
    client = fastapi.testclient.TestClient(app)
    assert client.get("/api/voices").status_code == 404


def test_voices_registered_when_enabled(app):
    cfg = Config(
        gradium=GradiumConfig(
            base_url="https://example.com/api",
            api_key=pydantic.SecretStr("test-key"),
        ),
    )
    gradbot.routes.setup(app, config=cfg, with_voices=True)
    client = fastapi.testclient.TestClient(app)
    resp = client.get("/api/voices")
    assert resp.status_code == 200
    assert "voices" in resp.json()


def test_index_served(app):
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "index.html").write_text("<h1>hi</h1>")
        gradbot.routes.setup(app, static_dir=d)
        client = fastapi.testclient.TestClient(app)
        assert "<h1>hi</h1>" in client.get("/").text


def test_index_404_when_missing(app):
    with tempfile.TemporaryDirectory() as d:
        gradbot.routes.setup(app, static_dir=d)
        client = fastapi.testclient.TestClient(app)
        assert client.get("/").status_code == 404


def test_bundled_js_served(app):
    with tempfile.TemporaryDirectory() as d:
        gradbot.routes.setup(app, static_dir=d)
        client = fastapi.testclient.TestClient(app)
        resp = client.get("/static/js/audio-processor.js")
        assert resp.status_code == 200
