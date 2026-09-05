"""Async client for Gradium's text-prompt voice-design workflow."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import pathlib
import re
import sqlite3
import struct
import time
import wave
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = frozenset({"en", "fr", "es", "pt", "de"})
MAX_DESCRIPTION_CHARS = 500
MAX_PREVIEW_CHARS = 100
TTS_MODEL_NAME = os.getenv("GRADIUM_TTS_MODEL_NAME", "gradium-tts-beta")
VOICE_DESIGN_CFG_SCALE = 10.0
VOICE_DESIGN_STEPS = int(os.getenv("VOICE_DESIGN_STEPS", "0")) or None
VOICE_DESIGN_HTTP_RETRIES = max(
    0, min(5, int(os.getenv("VOICE_DESIGN_HTTP_RETRIES", "2")))
)
VOICE_DESIGN_RETRY_DELAY_S = max(
    0.0, float(os.getenv("VOICE_DESIGN_RETRY_DELAY_S", "0.4"))
)
MAX_SEED = 2**31 - 1


class VoiceDesignError(RuntimeError):
    """A safe, actionable failure from the voice-design boundary."""


class DuplicateVoiceError(VoiceDesignError):
    """The candidate resolved to a voice Gradium has already promoted."""

    def __init__(self, existing_voice_id: str | None) -> None:
        super().__init__(
            "The requested change was too subtle to produce a different voice"
        )
        self.existing_voice_id = existing_voice_id


def fix_wav_sizes(audio: bytes) -> bytes:
    """Patch placeholder RIFF and data sizes in a streamed WAV response."""

    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio

    patched = bytearray(audio)
    struct.pack_into("<I", patched, 4, len(patched) - 8)
    position = 12
    while position + 8 <= len(patched):
        chunk_id = patched[position : position + 4]
        chunk_length = struct.unpack_from("<I", patched, position + 4)[0]
        if chunk_id == b"data":
            struct.pack_into("<I", patched, position + 4, len(patched) - position - 8)
            break
        if chunk_length in {0, 0xFFFFFFFF}:
            break
        position += 8 + chunk_length + (chunk_length & 1)
    return bytes(patched)


def wav_duration_s(audio: bytes) -> float:
    """Return the duration of a patched PCM WAV, or zero if it is unreadable."""

    try:
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            return wav_file.getnframes() / frame_rate if frame_rate else 0.0
    except (EOFError, wave.Error):
        return 0.0


class VoiceDesigner:
    """Generate candidates and manage the promoted voices used by Gradbot."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        store_path: pathlib.Path,
        http_timeout_s: float = 120.0,
        poll_timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._store_path = store_path
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s
        self._http_retries = VOICE_DESIGN_HTTP_RETRIES
        self._retry_delay_s = VOICE_DESIGN_RETRY_DELAY_S
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": api_key, "accept": "application/json"},
            timeout=http_timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def validate_request(
        description: str,
        preview_text: str,
        language: str,
        seed: int | None = None,
    ) -> None:
        if not description.strip():
            raise VoiceDesignError("Describe the voice before creating a preview")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise VoiceDesignError(
                f"Voice descriptions can be at most {MAX_DESCRIPTION_CHARS} characters"
            )
        if not preview_text.strip():
            raise VoiceDesignError("The preview line cannot be blank")
        if len(preview_text) > MAX_PREVIEW_CHARS:
            raise VoiceDesignError(
                f"Preview lines can be at most {MAX_PREVIEW_CHARS} characters"
            )
        if language not in SUPPORTED_LANGUAGES:
            choices = ", ".join(sorted(SUPPORTED_LANGUAGES))
            raise VoiceDesignError(f"Voice language must be one of: {choices}")
        if seed is not None and not (0 <= int(seed) <= MAX_SEED):
            raise VoiceDesignError(f"Seed must be an integer between 0 and {MAX_SEED}")

    async def generate_candidate(
        self,
        description: str,
        preview_text: str,
        language: str,
        *,
        seed: int | None = None,
    ) -> str:
        """Create one candidate, reusing a seed so revisions retain identity."""

        self._require_key()
        self.validate_request(description, preview_text, language, seed)
        json_config: dict[str, Any] = {"cfg_scale": VOICE_DESIGN_CFG_SCALE}
        if seed is not None:
            json_config["seed"] = int(seed)
        if VOICE_DESIGN_STEPS:
            json_config["steps"] = max(1, min(128, VOICE_DESIGN_STEPS))
        response = await self._request(
            "POST",
            "/voice-generator/generate",
            connect_retry=True,
            json={
                "prompt": description,
                "language": language,
                "n_samples": 1,
                "json_config": json_config,
            },
        )
        self._check(response, "Generating the voice preview")
        payload = self._json(response, "Voice generation")
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise VoiceDesignError("Voice generation returned no candidates")
        embedding_id = embeddings[0].get("embedding_id")
        if not isinstance(embedding_id, str) or not embedding_id:
            raise VoiceDesignError("Voice generation returned an invalid candidate ID")
        try:
            await self._wait_until_ready(embedding_id)
        except (httpx.HTTPError, VoiceDesignError):
            await self.delete_candidate(embedding_id)
            raise
        return embedding_id

    async def keep_candidate(
        self,
        embedding_id: str,
        *,
        name: str,
        description: str,
    ) -> str:
        """Promote a candidate and persist its candidate-to-voice mapping."""

        self._require_key()
        existing = self._lookup_voice(embedding_id)
        if existing:
            return existing
        response = await self._request(
            "POST",
            "/voices/from-embedding",
            connect_retry=True,
            json={
                "voxium_embedding_id": embedding_id,
                "name": name.strip() or "Designed voice",
                "description": description,
            },
        )
        if response.status_code == 409:
            raise DuplicateVoiceError(self._uid_from_conflict(response))
        self._check(response, "Saving the voice")
        payload = self._json(response, "Saving the voice")
        voice_id = payload.get("uid")
        if not isinstance(voice_id, str) or not voice_id:
            raise VoiceDesignError("Saving the voice returned no permanent voice ID")
        self._save_voice(embedding_id, voice_id, name, description)
        return voice_id

    async def render_preview(self, embedding_id: str, preview_text: str) -> bytes:
        """Synthesize a temporary candidate through the kit's REST preview route."""

        self._require_key()
        text = preview_text.strip()
        if not text:
            raise VoiceDesignError("The preview line cannot be blank")
        if len(text) > MAX_PREVIEW_CHARS:
            raise VoiceDesignError(
                f"Preview lines can be at most {MAX_PREVIEW_CHARS} characters"
            )
        response = await self._request(
            "POST",
            "/speech/tts",
            transport_retry=True,
            json={
                "text": text,
                "voice_id": embedding_id,
                "model_name": TTS_MODEL_NAME,
                "output_format": "wav",
                "only_audio": True,
            },
            headers={"accept": "audio/wav"},
        )
        self._check(response, "Rendering the voice preview")
        if not response.content:
            raise VoiceDesignError("Rendering the voice preview returned no audio")
        return fix_wav_sizes(response.content)

    async def delete_candidate(self, embedding_id: str) -> None:
        """Best-effort cleanup for a temporary candidate the user rejected."""

        if not self._api_key or not embedding_id:
            return
        try:
            response = await self._http.delete(
                f"/voice-generator/embeddings/{embedding_id}"
            )
            if response.status_code not in {204, 404}:
                self._check(response, "Cleaning up the previous voice preview")
        except (httpx.HTTPError, VoiceDesignError) as exc:
            logger.warning(
                "Could not clean up voice candidate %s: %s", embedding_id, exc
            )

    async def update_voice(
        self,
        voice_id: str,
        *,
        name: str,
        description: str,
    ) -> None:
        """Apply the user's final name and description to an active draft voice."""

        self._require_key()
        response = await self._request(
            "PUT",
            f"/voices/{voice_id}",
            transport_retry=True,
            json={"name": name.strip() or "Designed voice", "description": description},
        )
        self._check(response, "Naming the selected voice")

    async def delete_voice(self, voice_id: str) -> None:
        """Best-effort cleanup for an automatically promoted draft voice."""

        if not self._api_key or not voice_id:
            return
        try:
            response = await self._http.delete(f"/voices/{voice_id}")
            if response.status_code not in {200, 204, 404}:
                self._check(response, "Cleaning up the previous draft voice")
        except (httpx.HTTPError, VoiceDesignError) as exc:
            logger.warning("Could not clean up draft voice %s: %s", voice_id, exc)
            return
        with self._connect_store() as connection:
            connection.execute(
                "DELETE FROM voice_design_selections WHERE voice_id = ?", (voice_id,)
            )

    @staticmethod
    def _uid_from_conflict(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
            detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        except ValueError:
            detail = response.text
        match = re.search(r"embedding:\s*([A-Za-z0-9_-]+)", str(detail))
        return match.group(1) if match else None

    async def _wait_until_ready(self, embedding_id: str) -> None:
        deadline = time.monotonic() + self._poll_timeout_s
        while True:
            response = await self._request(
                "GET",
                "/voice-generator/embeddings",
                transport_retry=True,
                params={"embedding_id": embedding_id},
            )
            self._check(response, "Checking the voice preview")
            payload = self._json(response, "Voice preview status")
            embeddings = payload.get("embeddings")
            if not isinstance(embeddings, list) or not embeddings:
                raise VoiceDesignError("The generated voice preview disappeared")
            if embeddings[0].get("ready") is True:
                return
            if time.monotonic() >= deadline:
                await self.delete_candidate(embedding_id)
                raise VoiceDesignError(
                    f"Voice generation timed out after {self._poll_timeout_s:.0f} seconds"
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        connect_retry: bool = False,
        transport_retry: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Retry brief transport failures without duplicating accepted writes."""

        retryable = httpx.TransportError if transport_retry else httpx.ConnectError
        attempts = self._http_retries + 1 if connect_retry or transport_retry else 1
        for attempt in range(attempts):
            try:
                return await self._http.request(method, path, **kwargs)
            except retryable as exc:
                if attempt + 1 >= attempts:
                    raise
                delay = self._retry_delay_s * (2**attempt)
                logger.warning(
                    "Voice Design request connection failed; retrying %s %s "
                    "in %.1fs (%d/%d): %s",
                    method,
                    path,
                    delay,
                    attempt + 1,
                    attempts - 1,
                    exc,
                )
                await asyncio.sleep(delay)

        raise AssertionError("unreachable")

    def _require_key(self) -> None:
        if not self._api_key:
            raise VoiceDesignError("GRADIUM_API_KEY is required for voice design")

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceDesignError(f"{what} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise VoiceDesignError(f"{what} returned an unexpected response")
        return payload

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.is_success:
            return
        detail: Any = response.text.strip()
        try:
            payload = response.json()
            detail = (
                payload.get("detail", detail) if isinstance(payload, dict) else detail
            )
        except ValueError:
            pass
        if isinstance(detail, list):
            detail = "; ".join(str(item.get("msg", item)) for item in detail)
        suffix = f": {detail}" if detail else ""
        raise VoiceDesignError(f"{what} failed (HTTP {response.status_code}){suffix}")

    def _connect_store(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._store_path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_design_selections (
                embedding_id TEXT PRIMARY KEY,
                voice_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        return connection

    def _lookup_voice(self, embedding_id: str) -> str | None:
        with self._connect_store() as connection:
            row = connection.execute(
                "SELECT voice_id FROM voice_design_selections WHERE embedding_id = ?",
                (embedding_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def _save_voice(
        self,
        embedding_id: str,
        voice_id: str,
        name: str,
        description: str,
    ) -> None:
        with self._connect_store() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO voice_design_selections
                    (embedding_id, voice_id, name, description)
                VALUES (?, ?, ?, ?)
                """,
                (embedding_id, voice_id, name, description),
            )
