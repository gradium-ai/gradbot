# Notre IA

A simple French Gradbot voice chat. A French-speaking AI assistant answers
questions out loud.

It is built on the same shape as the `simple_chat` demo and talks to the hosted
Gradium APIs directly — no local ASR/TTS servers, no model downloads.

The browser captures microphone audio, streams it to `/ws/chat`, and plays the
assistant's spoken reply back. The UI is intentionally minimal: a title, an
animated avatar (a waving French flag or a friendly talking robot, switchable
in a hidden settings panel), and the mic button. The transcript appears once a
conversation starts.

## Run (combined launcher)

```bash
cd demos
cp config.example.yaml config.yaml   # then fill in your Gradium key
uv sync
uv run uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000/interview_questions/`, tap the mic, and ask your
questions in French.

## Run (standalone)

```bash
cd demos/interview_questions
cp config.example.yaml config.yaml   # then fill in your Gradium key
uv sync
uv run uvicorn main:app --reload --port 8007
```

Open `http://127.0.0.1:8007`.

## Config

`config.yaml` (gitignored) holds the LLM and Gradium settings. `config.load()`
reads `demos/interview_questions/config.yaml` first and falls back to
`demos/config.yaml`.

```yaml
llm:
  model: "google/gemma-4-26B-A4B-it"
  base_url: "https://gradium.ai/llm-server-19jPz0j1ZQ/v1"
  extra_config:
    chat_template_kwargs:
      enable_thinking: false

gradium:
  api_key: "your-gradium-key"
  base_url: "https://api.gradium.ai/api"
```

## Useful Env

| Variable | Purpose |
| --- | --- |
| `INTERVIEW_VOICE_ID` | Gradium voice id (default `jBULVCDhf05tOJN5`). |

## Smoke Test

```bash
uv run python -m py_compile main.py
```
