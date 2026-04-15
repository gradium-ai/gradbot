"""Standard route scaffolding for gradbot demos."""

from __future__ import annotations

import pathlib

import fastapi
import fastapi.responses
import fastapi.staticfiles

_JS_AUDIO_DIR = pathlib.Path(__file__).parent / "js_audio"


def setup(
    app,
    *,
    config: config.Config | None = None,
    static_dir: pathlib.Path | str | None = None,
    with_voices: bool = False,
) -> None:
    """Register standard demo routes on *app*."""
    from . import voices

    use_pcm = config.use_pcm if config else False
    base_url = config.gradium.base_url if config else None
    api_key = (
        config.gradium.api_key.get_secret_value()
        if config and config.gradium.api_key
        else None
    )

    if static_dir is not None:
        static_dir = pathlib.Path(static_dir)

    @app.get("/api/audio-config")
    async def audio_config():
        return fastapi.responses.JSONResponse(content={"pcm": use_pcm})

    if with_voices:

        @app.get("/api/voices")
        async def list_voices():
            return {"voices": await voices.load_catalog(base_url, api_key)}

    if static_dir is not None:
        app.mount(
            "/static/js",
            fastapi.staticfiles.StaticFiles(directory=_JS_AUDIO_DIR),
            name="bundled_js",
        )
        app.mount(
            "/static",
            fastapi.staticfiles.StaticFiles(directory=static_dir),
            name="static",
        )

        @app.get("/")
        async def index():
            index_path = static_dir / "index.html"
            if index_path.exists():
                return fastapi.responses.FileResponse(index_path)
            return fastapi.responses.JSONResponse(
                content={"error": "Frontend not found"},
                status_code=404,
            )
