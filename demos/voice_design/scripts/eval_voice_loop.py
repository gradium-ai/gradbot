#!/usr/bin/env python3
"""Run a real, speech-driven smoke test against the local Voice Workshop.

This intentionally exercises Gradium STT/TTS and Voice Design plus PhoneLLM.
It is a manual integration check, not part of the offline pytest suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

import websockets

HARPER_ID = "4SZHfMpw-p46Ywgs"


def synthesize_ogg(text: str, voice: str) -> bytes:
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if not say or not ffmpeg:
        raise RuntimeError("This check requires macOS `say` and `ffmpeg`")

    with tempfile.TemporaryDirectory(prefix="gradbot-eval-") as directory:
        root = pathlib.Path(directory)
        source = root / "turn.aiff"
        output = root / "turn.ogg"
        subprocess.run(
            [say, "-v", voice, "-r", "180", "-o", str(source), text],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-af",
                # Gradium's semantic end-of-turn padding can exceed a short
                # pause for a detailed request. Keep sending silence long
                # enough to exercise the same continuous stream as a live mic.
                "apad=pad_dur=8",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-application",
                "voip",
                "-b:a",
                "32k",
                str(output),
            ],
            check=True,
        )
        return output.read_bytes()


def synthesize_silence_ogg(duration_s: float = 3.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("This check requires `ffmpeg`")
    with tempfile.TemporaryDirectory(prefix="gradbot-eval-silence-") as directory:
        output = pathlib.Path(directory) / "silence.ogg"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                str(duration_s),
                "-c:a",
                "libopus",
                "-application",
                "voip",
                "-b:a",
                "32k",
                str(output),
            ],
            check=True,
        )
        return output.read_bytes()


def ogg_pages(data: bytes):
    position = 0
    while position < len(data):
        if data[position : position + 4] != b"OggS":
            raise RuntimeError(f"Invalid Ogg page at byte {position}")
        segment_count = data[position + 26]
        page_size = (
            27
            + segment_count
            + sum(data[position + 27 : position + 27 + segment_count])
        )
        page = data[position : position + page_size]
        granule = struct.unpack_from("<Q", page, 6)[0]
        yield page, granule
        position += page_size


class Conversation:
    def __init__(self, websocket, timeout_s: float) -> None:
        self.websocket = websocket
        self.timeout_s = timeout_s
        self.messages: list[dict[str, Any]] = []
        self.changed = asyncio.Condition()
        self.audio_bytes = 0
        self.reader_error: BaseException | None = None
        self.streaming_tts_active = False
        self.overlap_errors: list[str] = []

    async def read(self) -> None:
        try:
            async for raw in self.websocket:
                if isinstance(raw, bytes):
                    self.audio_bytes += len(raw)
                    continue
                message = json.loads(raw)
                self.messages.append(message)
                message_type = message.get("type")
                event = message.get("event")
                if message_type == "event" and event == "first_tts_audio":
                    self.streaming_tts_active = True
                elif message_type == "event" and event == "end_tts_audio":
                    self.streaming_tts_active = False
                elif message_type == "voice_feedback_audio" and self.streaming_tts_active:
                    self.overlap_errors.append(
                        f"custom voice audio arrived during streaming TTS: {message.get('text')}"
                    )
                if message_type == "agent_text":
                    print(f"  Assistant: {message.get('text')}", flush=True)
                elif message_type == "user_text":
                    print(f"  ASR: {message.get('text')}", flush=True)
                elif message_type in {"voice_design_status", "language_changed", "error"}:
                    print(
                        "  Event:",
                        json.dumps(message, ensure_ascii=False, sort_keys=True),
                        flush=True,
                    )
                elif message_type == "voice_feedback_audio":
                    print(
                        f"  Created voice: {message.get('text')} "
                        f"(revision {message.get('revision')}, "
                        f"{message.get('duration_s')}s)",
                        flush=True,
                    )
                elif message_type == "event" and event in {
                    "end_of_turn",
                    "end_tts_audio",
                }:
                    print(f"  Event: {event}", flush=True)
                async with self.changed:
                    self.changed.notify_all()
        except websockets.ConnectionClosed as exc:
            self.reader_error = exc
        except BaseException as exc:
            self.reader_error = exc
        finally:
            async with self.changed:
                self.changed.notify_all()

    async def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        start: int,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout_s or self.timeout_s)
        async with self.changed:
            while not any(predicate(message) for message in self.messages[start:]):
                if self.reader_error is not None:
                    raise RuntimeError(
                        f"WebSocket reader stopped: {self.reader_error}"
                    ) from self.reader_error
                for message in self.messages[start:]:
                    if message.get("type") == "error" or (
                        message.get("type") == "voice_design_status"
                        and message.get("status") == "error"
                    ):
                        raise RuntimeError(message.get("message") or "Agent error")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("The voice loop stopped making progress")
                await asyncio.wait_for(self.changed.wait(), remaining)
        return next(
            message
            for message in reversed(self.messages[start:])
            if predicate(message)
        )

    async def speak(self, text: str, voice: str = "Samantha") -> int:
        print(f"\nYou: {text}", flush=True)
        start = len(self.messages)
        # The browser sends this as soon as the first ASR text arrives. Sending it
        # just before synthetic audio avoids a timing race without changing state.
        await self.websocket.send(
            json.dumps({"type": "config", "user_activity": True})
        )
        await self.send_ogg(synthesize_ogg(text, voice))
        await self.wait_for_end_of_turn(start, 75)
        # Mirror the browser using the actual ASR transcript, never the intended
        # source sentence. Otherwise a language/STT failure can be hidden by the
        # evaluator feeding perfect text back into the app.
        recognized = self.user_spoken_since(start)
        if not recognized:
            raise AssertionError("ASR produced no transcript for the spoken turn")
        await self.websocket.send(
            json.dumps(
                {
                    "type": "config",
                    "user_activity": True,
                    "user_text": recognized,
                }
            )
        )
        return start

    async def wait_for_end_of_turn(self, start: int, timeout_s: float) -> None:
        """Keep the synthetic microphone alive until Gradbot closes the turn.

        A browser continuously sends mic frames. A finite Ogg recording can stop
        on the exact VAD frame that starts Gradbot's STT flush, leaving no later
        frame to complete it. Short chained silence tails also let a transient STT
        reconnect recover without making the evaluator report an app deadlock.
        """
        predicate = (
            lambda message: message.get("type") == "event"
            and message.get("event") == "end_of_turn"
        )
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ASR never completed the spoken turn")
            try:
                await self.wait_for(predicate, start, min(4.0, remaining))
                return
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("ASR never completed the spoken turn")
                await self.send_ogg(synthesize_silence_ogg(3.0))

    async def send_ogg(self, data: bytes) -> None:
        previous_granule = 0
        for page, granule in ogg_pages(data):
            if granule not in (0, 2**64 - 1) and previous_granule:
                await asyncio.sleep(
                    min(max((granule - previous_granule) / 48_000, 0), 1.1)
                )
            await self.websocket.send(page)
            if granule not in (0, 2**64 - 1):
                previous_granule = granule

    async def wait_for_feedback(self, start: int) -> dict[str, Any]:
        message = await self.wait_for(
            lambda item: item.get("type") == "voice_feedback_audio", start, 180
        )
        await asyncio.sleep(min(float(message.get("duration_s") or 0) + 0.8, 11))
        return message

    async def wait_for_spoken_reply(self, start: int) -> None:
        await self.wait_for(
            lambda message: message.get("type") == "agent_text", start, 75
        )
        await self.wait_for(
            lambda message: message.get("type") == "event"
            and message.get("event") == "end_tts_audio",
            start,
            75,
        )
        # Gradbot can deliver the final caption chunk just after end_tts_audio.
        # Humans naturally leave this margin after the audible response as well.
        await asyncio.sleep(1.5)

    def spoken_since(self, start: int) -> str:
        return " ".join(
            str(message.get("text") or "")
            for message in self.messages[start:]
            if message.get("type") == "agent_text"
        )

    def user_spoken_since(self, start: int) -> str:
        return " ".join(
            str(message.get("text") or "")
            for message in self.messages[start:]
            if message.get("type") == "user_text"
        )

    def assert_single_holding_phrase(self, start: int) -> None:
        speech = self.spoken_since(start).strip()
        if len([part for part in re.split(r"[.!?]+", speech) if part.strip()]) > 1:
            raise AssertionError(f"Stacked holding phrases were spoken: {speech!r}")

    def assert_no_tool_continuation_loop(self, start: int) -> None:
        completions = sum(
            message.get("type") == "event"
            and message.get("event") == "end_tts_audio"
            for message in self.messages[start:]
        )
        if completions > 3:
            raise AssertionError(
                f"Tool continuation loop emitted {completions} TTS completions"
            )


async def run(url: str, timeout_s: float, finalize: bool) -> None:
    async with websockets.connect(
        url,
        max_size=30_000_000,
        ping_timeout=30,
        close_timeout=5,
    ) as websocket:
        conversation = Conversation(websocket, timeout_s)
        reader = asyncio.create_task(conversation.read())
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "start",
                        "voice_name": "Harper",
                        "voice_id": HARPER_ID,
                        "language": "en",
                        "speed": 1.0,
                    }
                )
            )
            await conversation.wait_for(
                lambda message: message.get("type") == "event"
                and message.get("event") == "end_tts_audio",
                0,
                30,
            )
            await asyncio.sleep(1)
            opening = conversation.spoken_since(0).casefold()
            if opening.count("what kind of voice") > 1:
                raise AssertionError("The opening question was spoken more than once")

            start = await conversation.speak(
                "I want a Scottish voice of an old man in his seventies who is "
                "very grumpy and rude."
            )
            await conversation.wait_for_feedback(start)
            conversation.assert_single_holding_phrase(start)

            start = await conversation.speak(
                "Yes, it is, but I want the voice to be a bit more calming."
            )
            await conversation.wait_for_feedback(start)
            conversation.assert_single_holding_phrase(start)
            active = await conversation.wait_for(
                lambda message: message.get("type") == "voice_design_status"
                and message.get("status") == "active",
                start,
                5,
            )
            if active.get("revision") != 2:
                raise AssertionError("The spoken revision did not advance to draft 2")

            for expected_revision, edit in enumerate([
                "Yes, it is. Now make it a voice which is more knowledgeable and someone like the voice of someone who's lived and seen more life.",
                "Okay. I don't like how heavy it is. So I wanted Okay, let's reduce the age of the voice to mid 20s.",
            ], start=3):
                start = await conversation.speak(edit)
                await conversation.wait_for_feedback(start)
                conversation.assert_single_holding_phrase(start)
                active = await conversation.wait_for(
                    lambda message: message.get("type") == "voice_design_status"
                    and message.get("status") == "active", start, 5,
                )
                if active.get("revision") != expected_revision:
                    raise AssertionError(f"Descriptive edit stalled at draft {active.get('revision')}")

            start = await conversation.speak(
                "Can we talk about what makes a voice memorable?"
            )
            await conversation.wait_for_spoken_reply(start)
            if any(
                message.get("type") == "voice_design_status"
                and message.get("status") == "designing"
                for message in conversation.messages[start:]
            ):
                raise AssertionError("Ordinary conversation started a voice design")
            ordinary_reply = conversation.spoken_since(start).casefold()
            if not ordinary_reply or any(
                phrase in ordinary_reply
                for phrase in (
                    "get back to that",
                    "after we finish",
                    "system process",
                    "system hear",
                    "new request",
                )
            ):
                raise AssertionError(
                    f"Ordinary conversation was deflected: {ordinary_reply!r}"
                )

            start = await conversation.speak(
                "Please switch the conversation to French."
            )
            await conversation.wait_for(
                lambda message: message.get("type") == "language_changed"
                and message.get("language") == "fr",
                start,
                60,
            )
            confirmation = await conversation.wait_for_feedback(start)
            if "français" not in str(confirmation.get("text") or "").casefold():
                raise AssertionError("The French switch confirmation was not played")
            conversation.assert_no_tool_continuation_loop(start)

            start = await conversation.speak(
                "Garde la même voix, mais rends-la un peu plus chaleureuse.",
                "Thomas",
            )
            await conversation.wait_for_feedback(start)
            conversation.assert_single_holding_phrase(start)
            french_asr = conversation.user_spoken_since(start).casefold()
            if "chaleur" not in french_asr:
                raise AssertionError(
                    f"French ASR did not recognize the requested change: {french_asr!r}"
                )
            french_filler = conversation.spoken_since(start).casefold()
            if any(
                phrase in french_filler
                for phrase in ("i'll", "i will", "let me", "let's", "one moment")
            ):
                raise AssertionError(
                    f"French revision used an English filler: {french_filler!r}"
                )
            active = await conversation.wait_for(
                lambda message: message.get("type") == "voice_design_status"
                and message.get("status") == "active",
                start,
                5,
            )
            if active.get("revision") != 5:
                raise AssertionError("The French revision did not advance to draft 5")

            start = await conversation.speak(
                "Passe la conversation en anglais, s'il te plaît.", "Thomas"
            )
            await conversation.wait_for(
                lambda message: message.get("type") == "language_changed"
                and message.get("language") == "en",
                start,
                60,
            )
            confirmation = await conversation.wait_for_feedback(start)
            if "english" not in str(confirmation.get("text") or "").casefold():
                raise AssertionError("The English switch confirmation was not played")
            conversation.assert_no_tool_continuation_loop(start)

            start = await conversation.speak(
                "Please use your original agent voice again."
            )
            confirmation = await conversation.wait_for_feedback(start)
            if "original voice" not in str(confirmation.get("text") or "").casefold():
                raise AssertionError("Switching to the original voice was not confirmed")

            start = await conversation.speak(
                "Now go back to the designed voice."
            )
            confirmation = await conversation.wait_for_feedback(start)
            if "designed voice" not in str(confirmation.get("text") or "").casefold():
                raise AssertionError("Switching to the designed voice was not confirmed")

            if finalize:
                start = await conversation.speak(
                    "I like it. Please keep this voice and call it Angus."
                )
                await conversation.wait_for(
                    lambda message: message.get("type") == "voice_design_status"
                    and message.get("status") == "finalized",
                    start,
                    90,
                )
                confirmation = await conversation.wait_for_feedback(start)
                if "keep this voice" not in str(
                    confirmation.get("text") or ""
                ).casefold():
                    raise AssertionError("Keeping the voice was not confirmed")

            all_speech = " ".join(
                message.get("text", "")
                for message in conversation.messages
                if message.get("type") == "agent_text"
            ).casefold()
            for forbidden in (
                "i'm still working on that voice",
                "i didn't catch that",
                "[empty response]",
            ):
                if forbidden in all_speech:
                    raise AssertionError(f"Repeated/fallback phrase spoken: {forbidden}")
            if conversation.overlap_errors:
                raise AssertionError("; ".join(conversation.overlap_errors))

            print(
                "\nPASS: complete speech loop finished without freezes or repeats "
                f"({conversation.audio_bytes} output-audio bytes).",
                flush=True,
            )
            await websocket.send(json.dumps({"type": "stop"}))
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/chat")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Also keep the generated test voice instead of deleting it on close",
    )
    args = parser.parse_args()
    asyncio.run(run(args.url, args.timeout, args.finalize))


if __name__ == "__main__":
    main()
