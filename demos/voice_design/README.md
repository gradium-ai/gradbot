# Voice Design

A speech-first Gradbot demo for designing a Gradium voice conversationally.

You talk to a voice design director. It asks what you want the voice to sound
like, creates a Gradium Voice Design candidate, and changes the live conversation
to that voice. The director's next feedback question is the audition: there is no
separate assistant-voice preview. Harper starts the call and returns only when the
user explicitly asks to hear the original agent voice.

The LLM is [PhoneLLM Alpha 1](https://huggingface.co/pipecat-ai/phonellm-alpha-1)
via any OpenAI-compatible endpoint.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Rust toolchain — `gradbot` is built from source with maturin
- A Gradium API key (STT, TTS, and Voice Design all use it)
- A running PhoneLLM OpenAI-compatible server (see below)

## Setup

```bash
cd demos/voice_design
uv sync
```

`gradbot` is built from this repository's `gradbot_py` source with maturin, so
the first sync can take a few minutes. After Rust changes, run
`uv sync --reinstall-package gradbot`; a plain `uv sync` may reuse the existing
wheel when the package version has not changed.

## Run

Create a git-ignored `.env` file:

```dotenv
GRADIUM_API_KEY=your_key_here
LLM_BASE_URL=http://localhost:8001/v1
```

Then run:

```bash
uv run uvicorn main:app --reload --env-file .env
```

Then open http://localhost:8000. When running the combined demos application,
open `/voice_design/` instead.

## The PhoneLLM endpoint

`LLM_BASE_URL` is required — the demo ships no endpoint of its own. Leave it
unset and gradbot falls through to OpenAI's API, which fails against the
placeholder `api_key: "unused"`.

Start PhoneLLM's vLLM server with automatic tool choice and the `qwen3_coder`
parser; the demo relies on native OpenAI tool calls:

```bash
vllm serve "pipecat-ai/phonellm-alpha-1" \
  --port 8001 \
  --trust-remote-code \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --default-chat-template-kwargs '{"enable_thinking":false}' \
  --override-generation-config '{"temperature":0}'
```

Point `LLM_BASE_URL` at the `/v1` path of wherever that server is reachable —
`http://localhost:8001/v1` locally, or an ngrok/Cloudflare tunnel URL if you're
running it on a remote GPU box. Keep tunnel URLs out of the repo: an
unauthenticated tunnel URL is effectively a credential for that server.

## Configuration

`config.example.yaml` is loaded automatically and pins the LLM defaults:

| Setting | Value |
| --- | --- |
| Model | `pipecat-ai/phonellm-alpha-1` |
| Temperature | `0` |
| Thinking | disabled, per the PhoneLLM model card |
| `silence_timeout_s` | `0.0` — a pause won't make the director speak unprompted |

Create a local `config.yaml` to override any of it. `config.yaml` is
git-ignored, so it's the right place for a real `gradium.api_key`.

`base_url` is deliberately absent from the YAML: gradbot's config loader only
applies `LLM_BASE_URL` when the YAML key is missing, so putting it back would
silently shadow the env var.

Operational timeouts, if you need them:

```bash
export GRADIUM_TTS_MODEL_NAME=gradium-tts-beta
export VOICE_DESIGN_HTTP_TIMEOUT_S=120
export VOICE_DESIGN_POLL_TIMEOUT_S=120
export VOICE_DESIGN_POLL_INTERVAL_S=1
# Optional: fewer generator steps trade some quality for lower latency.
export VOICE_DESIGN_STEPS=12
# How long a safely prefetched revision waits for PhoneLLM's matching tool call.
export VOICE_DESIGN_PREFETCH_CLAIM_TIMEOUT_S=5
```

## How the voice design flow works

Each spoken revision runs Gradium's candidate workflow:

1. `POST /voice-generator/generate` creates one temporary candidate
   (`cfg_scale: 10`, `n_samples: 1`). One random seed is chosen for the call and
   reused for every revision so the speaker's identity remains recognisable.
2. `GET /voice-generator/embeddings` is polled until that candidate is ready.
3. Once ready, preview TTS and `POST /voices/from-embedding` run concurrently.
   The tool call is acknowledged before this long work begins, preventing
   Gradbot's pending-tool continuation from producing repeated tool calls.
4. Gradbot installs the promoted voice `uid`, then the browser plays a localized
   feedback question synthesized directly with the designed voice. This avoids
   depending on an occasionally empty PhoneLLM tool-result continuation.
5. Replaced drafts are retired only after the current streamed filler and direct
   feedback audio finish. Unfinalized drafts are deleted when the call ends; an
   explicitly kept voice is retained.

Descriptions are capped at 500 characters. Common one-trait revisions—gender,
age, accent, pitch, pace, effort, and mood—edit the current description in place.
Unknown changes are appended as authoritative. Only short or explicitly phrased
edits are prefetched at end-of-speech; conversational questions never trigger a
Voice Design request. Candidate-to-voice mappings are stored in the git-ignored
`voice-design-selections.sqlite3` file.

Before generation, PhoneLLM may use one short, mindful acknowledgement through
the currently active voice. It must not paraphrase the request, stack fillers, or
reuse an acknowledgement from the conversation. After generation it asks one of
several rotating feedback questions through the new voice.

## Conversation flow

1. Start the call with Harper, the default English (US) voice.
2. Describe the voice you want — "warm, lower, and more deliberate."
3. PhoneLLM may briefly acknowledge the request while the voice is generated.
4. The new voice asks how it sounds; subsequent revisions continue in the latest
   designed voice and preserve every trait the user did not change.
5. Say "use your original voice" to return to Harper, or "use the designed voice"
   to switch back.
6. Say "keep this voice" to retain the current voice. A kept voice can still be
   used as the starting point for another revision in the same call.

The UI shows the active description, revision, generation state, final voice ID,
live transcript, echo-cancellation setting, and a response-speed control.

Explicit commands such as "switch to French" update the conversation language,
STT, TTS, future voice designs, and the direct feedback questions together. A
request for a "French voice" remains a voice-design trait rather than switching
the conversation language.

## Tests

```bash
uv run --extra test pytest
```

The suite covers the request/response boundary with `httpx.MockTransport` — no
network and no API key needed.
