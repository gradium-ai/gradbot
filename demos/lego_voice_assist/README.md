# LEGO Voice Assist

A hands-free voice companion for building LEGO. The official app tells you *what*
to build; this tells you **where the piece is in the jungle** — out loud, from
your point of view.

Load a set's instruction PDF and the app becomes the guide: it renders the pages
on screen, so the step you're looking at is always the current step. A webcam
watches only your messy pile of bricks. Say "next" and a vision model finds this
step's pieces in the pile and the voice tells you where they are:

> "You need two dark red two by four bricks — close to you, on your right, next to the grey baseplate."

It's a showcase for [Gradium](https://gradium.ai)'s voice stack (streaming STT ↔
conversational LLM ↔ TTS via [gradbot](https://pypi.org/project/gradbot/)) in a
setting where your hands and eyes are busy and talking is the only free channel.

## Features

- **Voice-first**: tap the mic and just talk — "next", "find the red brick", "how many do I need?", "is this the right piece?"
- **The app is the guide**: the instruction PDF renders on screen, so the current step is deterministic — no camera reads a booklet, no page-flipping to detect.
- **Pinpoint pile search**: a vision LLM localizes only the few pieces the step needs, and the UI draws a tight glowing outline right on each brick.
- **Egocentric directions**: locations are spoken from *your* seat ("close to you, on your left") — computed in code from a labeled 3×3 grid, never guessed by the model.
- **Look-ahead latency**: landing on a step prefetches the next one's pile search, so "next" answers instantly — one background vision call per step, zero in the ask path.
- **Multilingual**: English, French, Spanish, German, Portuguese — the voice swaps mid-call.

## Setup

You need two API keys:

- **`GRADIUM_API_KEY`** — for streaming speech-to-text and text-to-speech ([get one at gradium.ai](https://gradium.ai)).
- **`OPENAI_API_KEY`** — for the vision model that finds pieces and parses the manual, and for the conversational LLM.

Both keys stay server-side; the browser never sees them.

```bash
cp .env.example .env          # then fill in the two keys above
uv sync                       # install dependencies
uv run uvicorn main:app --reload --port 8410
```

Open **http://localhost:8410** — localhost is a secure context, so the webcam and
mic work without HTTPS.

## Usage

1. **Upload your instructions PDF** in the Guide pane. LEGO publishes official PDFs
   for every set; the app parses each page once (one vision call per page, cached
   in `data/`), then renders the pages on screen.
2. Under **Settings**, tell it where your camera is relative to you —
   *Camera faces me* (webcam on the monitor) or *Camera behind me / over my
   shoulder*. This is what makes "left" mean *your* left.
3. **Tap the mic** and start the conversation. Try:
   - "Let's build" / "next" — advance a step and hear where its pieces are
   - "Find the long grey bar" — locate one specific piece
   - "Is this the right one?" — hold a brick up to the camera to check it
   - "How many do I need?" — answered from context, no re-scan
4. Watch the glowing highlight on the pile, grab the brick, build. Repeat.

The on-screen prev/next arrows and voice "next" stay in lockstep — both drive the
same step.

## How it works

One FastAPI process ([`main.py`](main.py)) serves the single-page frontend
([`static/index.html`](static/index.html)) and orchestrates two upstream stacks:

- **Gradium voice** (via gradbot): a session per call streaming Gradium STT ↔ a
  conversational LLM ↔ Gradium TTS, with turn-taking and barge-in. The LLM never
  sees images — it calls tools (`next_step`, `find_step`, `find_one_piece`,
  `check_piece`, `switch_language`).
- **OpenAI vision** ([Responses API](https://developers.openai.com/api/docs/guides/images-vision)): the tool handlers grab a fresh pile frame,
  burn a labeled 3×3 grid into it, and ask the model to localize *only* this
  step's known pieces. Cells become spoken directions in deterministic Python,
  and the agent relays that sentence verbatim.

The core trick: you never identify *every* piece in the pile. The parsed manual
says which few pieces this step needs, so the vision model only has to find
those — a tractable problem where "find everything" is not.

See [AGENTS.md](AGENTS.md) for the full architecture, the latency design, and the
gotchas worth knowing before you change anything.

**Learn more:** [gradbot](https://github.com/gradium-ai/gradbot) — the open-source
voice framework this is built on · [OpenAI — Images and vision](https://developers.openai.com/api/docs/guides/images-vision)
— how the pile search passes images to the model (Responses API, base64 input, and
the `detail` levels the demo tunes via `PILE_IMAGE_DETAIL`).

## Configuration

Only the two keys are required. Everything else has a sensible default — see
[`.env.example`](.env.example) for the full list, including model/effort tiers,
per-language voice overrides, and the STT flush window.

## Tests

Unit tests cover the deterministic helpers — grid-cell parsing, egocentric
direction phrasing, speech composition — the logic the module's own comments
insist stay "exact code, never left to a model." They stub the native
`gradbot` extension so they run without building the Rust workspace:

```bash
uv run --group dev pytest tests
```

## Hosting

Like the other gradbot demos, this ships no deploy files of its own — it's a
plain FastAPI app (`main:app`) served by the shared gradbot deployment when it
lives under `demos/`. If you self-host it standalone, two things matter: it needs
**HTTPS** (the webcam and mic require a secure browser context), and the parsed-
manual cache in `data/` is not persistent unless you mount a volume at
`/app/data` — otherwise a restart re-parses on the next upload (~25 s).
