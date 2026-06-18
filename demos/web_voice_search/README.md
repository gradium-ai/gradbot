# Voice Web Search — Gradium + Keenable

A fast, focused voice agent: ask a question out loud, and the agent searches the
live web via [Keenable](https://docs.keenable.ai/api-reference/search) (realtime
mode) and answers in a sentence or two — while streaming the source cards to the
browser. Inspired by https://openai.keenable.ai/.

## What it does

- **Voice in / voice out** via Gradium (speech-to-text + text-to-speech).
- A single `web_search` tool backed by the Keenable Search API in **realtime
  mode** (the lowest-latency mode).
- The agent searches immediately for anything factual or current instead of
  guessing, then answers concisely and names the source.
- The browser shows the live conversation and the web sources as they come in.

## Run locally

```bash
cd demos/web_voice_search
uv sync
cp .env.example .env   # fill in the three keys below
uv run uvicorn main:app --reload --port 8060
```

Open http://localhost:8060/ and tap the mic.

## Configuration (`.env`)

| Variable | Required | Notes |
| --- | --- | --- |
| `KEENABLE_API_KEY` | Yes | Keenable web search. Get one at https://keenable.ai. |
| `GRADIUM_API_KEY` | Yes | Gradium voice (STT + TTS). |
| `LLM_API_KEY` | Yes | OpenAI-compatible LLM key. **Must support tool calling.** |
| `LLM_BASE_URL` | No | Defaults to OpenRouter (`https://openrouter.ai/api/v1`). |
| `LLM_MODEL` | No | Defaults to `openai/gpt-4o-mini` (fast + tool-capable). |

`main.py` calls `load_dotenv()` so these `.env` values populate the environment
that gradbot and the Keenable client read from.

> Note: the conversational LLM must support function/tool calling. Some models
> (e.g. `mistral-small` on certain providers) have no tool-capable endpoint and
> will fail with a 404 — `openai/gpt-4o-mini` is a safe default.

## How it's built

- `main.py` — FastAPI app, the gradbot voice session, the `web_search` tool
  definition, and the `on_tool_call` handler that calls Keenable and streams
  results to both the LLM and the browser.
- `keenable_search.py` — small async client for `POST /v1/search` (realtime).
- `static/index.html` — mic UI, live transcript, and the web-sources panel
  (reuses gradbot's bundled audio JS served under `static/js/`).
