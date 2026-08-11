# LEGO Voice Assist — agent guide

Screen guide + vision LLM + Gradium voice: **the app IS the guide.** The parsed
manual's pages render on screen (the physical booklet leaves the loop), so the
step you're looking at is deterministically the current step — no camera reads a
booklet. A webcam watches only your messy PILE; the voice companion finds this
step's pieces in it and says where they are out loud — from *your* point of view
("close to you, on your right"). The official LEGO app tells you what to build;
this tells you **where the piece is in the jungle**, hands-free. The whole thing
is a showcase for Gradium's voice stack (STT ↔ conversational LLM ↔ TTS via
gradbot) in a hands-busy, eyes-busy setting.

<!-- gradium:generated -->
## Repo map

- `main.py` — the whole backend (FastAPI). Vision calls (`_vision_json`, Responses
  API), prompts, `/api/find` (pile search — backend smoke path), `/api/manual`
  (PDF parse + cache + per-page raster persist), `/api/manual/{id}/page/{n}`
  (serves a rasterized page — the on-screen guide's image), egocentric direction
  composition (`_egocentric_phrase`/`_build_speech`), `/ws/chat` (gradbot voice
  session: streaming Gradium STT/TTS + conversational LLM + tools `next_step` /
  `find_step` / `find_one_piece` / `check_piece` / `switch_language`),
  `/api/frames/{sid}` (pile-crop mailbox; `warm` prefetches the current step,
  `step: N` sets `state.step_idx` and prefetches step N — the on-screen prev/next
  control). `_prefetch(state, idx)` is the latency play: one pile search for the
  known step, then a look-ahead find for step N+1 so the next "next" is a cache
  hit.
- `static/index.html` — the whole frontend, single file, no build step: three
  panes — Companion (mic + last agent line + language buttons + settings), Guide
  (parsed manual pages rendered on screen, prev/next), Pile (webcam = the whole
  frame; 3×3 grid overlay + pinpoint highlights, driven by `setInterval` not rAF).
  gradbot call UI (SyncedAudioPlayer), capture round-trip (pile crop + full-frame
  `scene` for check_piece), manual upload/restore.
- `data/` — parsed-manual cache: `<id>.json` (parsed steps) + `<id>/page-NNN.jpg`
  (page renders), gitignored. `e2etestmanual.json` is a synthetic 3-step fixture;
  its `e2etestmanual/page-00N.jpg` renders back the on-screen guide in the harness.
- `demo/` — local screen recordings (gitignored, 65MB+; never commit).
- `.claude/launch.json` — dev-server launch config (autoPort; falls back when
  8410 is taken).
- `.env` (gitignored) / `.env.example` — `GRADIUM_API_KEY`, `OPENAI_API_KEY`,
  optional `OPENAI_MODEL`, `OPENAI_FAST_MODEL`, `OPENAI_REASONING_EFFORT`,
  `OPENAI_FIND_EFFORT`, `GRADIUM_VOICE_ID` (+ `GRADIUM_VOICE_ID_FR`/`_ES`/
  `_DE`/`_PT` per-language overrides), `GRADIUM_API_URL`, `GRADBOT_FLUSH_S`
  (STT flush window, default 0.3 — the end-of-speech→answer lever), and for
  the gradbot session `LLM_MODEL`/`LLM_BASE_URL`/`LLM_EXTRA_CONFIG` (defaults
  are derived from the OpenAI vars).

## Commands

```bash
uv sync                                         # install deps
uv run uvicorn main:app --reload --port 8410    # run (any port works)
uv run python -c "import main"                   # fast backend sanity check
```

No test suite and no linter are configured. Verification is manual — see
"How we verify" below.
<!-- /gradium:generated -->

## Architecture pointers

One FastAPI process serves the static page and two upstream stacks: OpenAI
vision (Responses API) for all pile lookups, and a gradbot session per call
(Rust multiplexer streaming Gradium STT ↔ conversational LLM ↔ Gradium TTS,
with turn-taking and barge-in). The conversational LLM never sees images — it
calls tools (`next_step`, `find_step`, `find_one_piece`, `check_piece`,
`switch_language`); tool handlers fetch fresh pile crops via a capture
round-trip (websocket `capture` event → browser POSTs crops to
`/api/frames/{sid}`), run the vision pipeline, and return a pre-composed `say`
sentence the agent relays verbatim. All keys stay server-side. The core design
insight: never identify *every* piece in the pile — a parsed manual is required,
and it says which few pieces the current step needs; the vision model only
localizes those on a labeled 3×3 grid burned into the pile crop — each copy as
`{cell, box}`, where the tight normalized bounding box drives a pinpoint
on-screen highlight (measured 0.1–0.2 % center error at high image detail) and
the cell stays authoritative for speech and as the visual fallback: a box only
survives `_piece_cells` if its centroid lands in the reported cell, which
provably drops exactly the hallucinated boxes. Deterministic code, never a
model, converts grid cells into builder-relative directions.

**The step is on the screen, not in the camera.** `_parse_manual` rasterizes
every PDF page and persists it (`data/<id>/page-NNN.jpg`); the guide renders
those pages, so the current step is deterministic (`state.step_idx`) — no spread
reads, no page-flip detector, no step matching, no camera-only mode. Voice
"next" and the on-screen prev/next control converge on the SAME server-side
index: prev/next POSTs `{step: N}` to the frames mailbox (→ `step_idx = N` +
prefetch); voice `next_step` advances the index and pushes the new page +
highlights back to the screen via a `find_result` message.

**The latency play (`_prefetch`), now trivially exact:** because the step is
known there is no guessing — one pile search composes the whole answer for step
`idx`, caches it in `state.find_cache` keyed by flat-step index (180 s TTL,
`PREFETCH_FRESH_S` — the pile only changes after the builder *hears* where the
pieces are), and pushes the highlights to the screen before the builder asks.
Then `_warm_ahead` fires a look-ahead find for step `idx+1` on the same pile
frame, so the eventual `next_step` is a cache hit returning in microseconds.
Tools join any in-flight prefetch (`_await_prefetch`) rather than racing it.
Ad-hoc (`find_one_piece`) cache entries clear on every step change; live-capture
entries keep the short 20 s TTL. Result: ~1 vision call per step in the
background, zero vision in the ask path.

## Repo-specific conventions

- **Verify locally + desk-test with a real set BEFORE opening a PR.** The pile
  search only proves out against real bricks under real light; only after a desk
  test do we push and `gh pr create`. This overrides the default
  branch→PR-immediately flow.
- Directions spoken to the user are computed in deterministic Python
  (`_egocentric_phrase` and friends in `main.py`) from grid cells + the
  camera-position preset — vision prompts FORBID the model from writing
  "left"/"top" etc., and the voice agent's prompt orders it to relay the
  tool's `say` sentence verbatim. Keep both; a model that mirrors wrong is
  worse than useless.
- Vision model/effort tiering: cheap tier (`OPENAI_FAST_MODEL`) first
  everywhere; the flagship (`OPENAI_MODEL`) only comes in as a one-shot
  escalation for pile searches and piece checks. Escalation triggers on ANY
  needed piece with an empty copies list — the fast tier misses obvious bricks
  while reporting 0.95 confidence, so a confident empty is not evidence of
  absence. **But escalation is the SLOW path (two sequential calls) and on a
  dense real pile it fires almost every time, so it is gated to the BACKGROUND
  only:** `_find_listed(escalate=False)` on the LIVE ask path (`_locate`) does a
  single fast call (~3 s), while the prefetch/look-ahead runs `escalate=True`
  (~8 s, hidden). Reasoning effort is LOW for both pile search (`OPENAI_FIND_
  EFFORT`) and parsing (`OPENAI_PARSE_EFFORT`) — these are perception, not logic,
  and high effort is pure latency (and where responses go `incomplete` → the 2×
  retry). Parsing fans out at `PARSE_CONCURRENCY` (12) — a 76-page manual parses
  in ~25 s at low effort vs minutes at high. Bump `OPENAI_PARSE_EFFORT=high` for
  a careful re-parse if counts come out wrong (low still read a printed "2x"
  exactly in testing).
- `PILE_IMAGE_DETAIL` defaults to "high": at low detail the fast tier both
  misses bricks and hallucinates boxes (22 % center error measured); at high
  detail boxes are near-pixel-perfect on BOTH tiers with no measured latency
  cost — the price is image tokens only.
- Speech strings must be TTS-friendly: number words, "2 by 4" not "2x4",
  semicolons between location phrases (they contain commas).

## How we verify (no test suite)

- Browser-level: stub `getUserMedia`/`enumerateDevices` with a canvas
  `captureStream` for video and a `MediaStreamDestination` for the mic, drive
  the real UI handlers via `javascript_tool`. For a zero-human end-to-end
  test, synthesize an utterance with macOS `say` (WAV, 24 kHz mono;
  `say -o u.wav --data-format=LEI16@24000 "let's build"`), load it into the
  page (decode base64 → `AudioContext.decodeAudioData` → a `BufferSource`
  into the mic destination), and watch STT → tool call → vision → verbatim
  relay land in the agent line and the server log (`find cache HIT ('step', N)`
  proves the prefetch/look-ahead paid off). This harness caught every
  regression so far — reuse it. THE TRAP: the preview pane keeps pages
  `document.hidden`, so `requestAnimationFrame` NEVER fires — drive the fake
  camera with `canvas.captureStream(0)` + a `setInterval` calling
  `track.requestFrame()`, and the overlay draw loop is a `setInterval` too (not
  rAF) for the same reason (audio playback exempts the tab from deep timer
  throttling). The pile is the WHOLE camera frame now — no region to drag, so
  the old PointerEvent trap is gone. `data/e2etestmanual.json` +
  `data/e2etestmanual/page-00N.jpg` (synthetic PIL page renders) +
  `localStorage.legoManual = "e2etestmanual"` gives the session a known 3-step
  manual whose pages the guide can actually show. NOTE highlights carry a 30 s
  TTL — read/screenshot them promptly, slow multi-step probing outruns it.
- Backend: real API smoke calls against synthetic PIL-drawn piles (bricks at
  known grid cells) via `POST /api/find` with an explicit `pieces` list. Keys
  are in `.env`, calls cost cents.
- Then the desk test: a real set, a camera facing the pile (an overhead phone
  cam works well), and an actual spoken build.

## Gotchas

- **A parsed manual is now REQUIRED — there is no camera-only mode.** The guide
  IS the parsed pages; with no manual there is nothing to show and nothing to
  find, so every tool returns "load your instructions first." The booklet never
  enters the camera, so there is no `bookCrop`, no 180° rotation, no spread read.
- **The pile crop is the WHOLE camera frame** (grid burned in), and its grid
  cells map straight to on-screen highlight positions — never rotate or region
  it. `scene` (the same frame, no grid) is what `check_piece` looks at.
- **Page renders are persisted at parse time** to `data/<id>/page-NNN.jpg` and
  served by `/api/manual/{id}/page/{n}`; `_load_flat_steps` tags each step with
  its physical page index (`img`). A manual parsed BEFORE this feature has a
  `<id>.json` but no image dir — the guide falls back to a step-number card
  (`has_images: false`), since regenerating needs the PDF bytes we don't store.
- **On-screen prev/next and voice "next" must stay in lockstep.** Both drive
  `state.step_idx`; the browser POSTs `{step: N}` to `/api/frames/{sid}` (gradbot
  only routes start/config/stop on the ws, so custom client→server messages ride
  the HTTP mailbox), and the server pushes `find_result` (with the step index +
  `image_url`) back so the guide follows a voice-driven advance.
- **`data/` cache is keyed by PDF content only** — not by model or prompt. After
  changing the parse prompt or model, `rm -rf data/<id>*` (json AND page dir) and
  re-upload, or you'll silently serve stale parses.
- **A manual with zero steps must never be adopted** — the client refuses it
  (a wrong-file upload once bricked every lookup with misleading errors).
- **Responses API JSON mode requires the word "json" in the *input* messages**;
  the system prompt therefore rides in `input` as a developer message, not in
  `instructions`.
- **Reasoning can eat the whole `max_output_tokens` budget** → `incomplete`
  status, empty text. `_vision_json` retries once at 2×; budget generously.
- **Multi-copy pieces need `grid_cells` (array).** With a single-cell schema the
  model honestly nulls the location and smuggles the truth into `notes`, which
  gets spoken verbatim.
- **Don't stack PRs with `--base <feature-branch>`** unless merge order is
  guaranteed — a child PR merged into its base after the base already merged to
  main once silently dropped a whole upgrade. Prefer rebasing onto main.
- Any port works; pass `--port` if the default is taken.
- **gradbot's LLM goes through `/v1/chat/completions`, which rejects function
  tools + `reasoning_effort` on gpt-5.6 models** (400). `LLM_EXTRA_CONFIG`
  must keep `{"reasoning_effort": "none"}` — also the right latency call for
  a voice loop. The failure mode is a session that connects and then says
  nothing.
- **Call `gradbot.init_logging()` or Rust-side errors vanish** — the greeting
  simply never arrives and nothing is logged. `DEBUG=1 RUST_LOG=info` when
  debugging sessions.
- **The agent cannot speak unprompted** (gradbot turns start from the user, no
  server-initiated turns), so an on-screen prev/next (or a warm-up) only
  prefetches — answer cached, highlights + status hint on screen; the builder
  still says "next" to hear it. Don't promise proactive announcements.
- **Live language switch** (`switch_language` tool, or the language buttons →
  `{type:"config", lang}`) rebuilds the session config with a per-language
  flagship voice + translated instructions + per-language spoken-style guidance
  (`LANGUAGE_STYLE_GUIDANCE`, BAD/GOOD direction phrasing), via
  `input_handle.send_config` — which swaps voice/instructions WITHOUT re-greeting
  (`should_greet=false`). The tool returns a `message` (not a `say`) so the agent
  picks its own words in-language.
- **Cached answers still speak in two beats**: the LLM streams its short ack
  ("On it."), that TTS turn ENDS before the ~0 ms tool result gets processed,
  and the say arrives in a follow-up generation ~1.5–2 s later. That's the
  multiplexer's pending-tool-results design, not a cache miss — don't chase
  it.
- **Anything you put in the agent prompt as a quoted example WILL be spoken
  verbatim** — a non-English session greeted bilingually because the sample
  greeting was English. Keep literal examples out of non-English prompts
  (`_agent_prompt` gates the greeting sample on `lang == "en"`).
- **Tool descriptions must order the call, not describe preconditions.**
  "Tell them to hold the piece up first" made the agent lecture instead of
  calling check_piece; the fix is "call IMMEDIATELY — the result handles a
  piece the camera can't see."
- **`find_cache` is keyed by flat-step INDEX, not printed step number** —
  numbering restarts across sub-builds, and prefetched entries live 180 s,
  long enough for a collision to bite. Ad-hoc entries clear on every step change.
- **"Stuck on 'one moment' forever" = a slow vision call behind the ack, not a
  hang.** The agent streams a filler ("One moment.") then waits for the tool.
  The culprit was fast→flagship escalation on the LIVE path (2 calls, 12–21 s)
  plus the `incomplete`→2× retry (one prefetch hit 45 s). Fixes: live path is
  single-tier (`escalate=False`, ~3 s), escalation is background-only, effort is
  LOW, voice "next" JOINS the in-flight look-ahead (`_await_prefetch` gathers
  `warm_tasks`) instead of racing it, and the OpenAI client has a 60 s timeout so
  a truly wedged call fails to a spoken error instead of hanging. If it feels
  slow again, check the log for `live lookup took Ns` and whether escalation is
  firing on the live path.
- Manual state is loaded into the session at call start (`manual_id` in the
  start message). A PDF uploaded mid-call needs a call restart to be seen.

## Vision & roadmap

**Why this exists:** the genuinely missing piece in LEGO building is *finding
pieces in the pile* — the official app already tells you what to build. It's
also a showcase for Gradium STT/TTS in a hands-busy, eyes-busy setting where
voice is the only free channel. Demo ethos: prove the magic, rough edges OK.

Design direction: conversational-first; the GUI earns its place only where it's
functional (rendering the guide, highlighting pieces). The same recipe
generalizes to other guided, hands-busy tasks — hardware assembly (e.g.
Raspberry Pi kits), guided puzzles (e.g. a Rubik's cube) — but those are
follow-up ideas, not this repo's scope.

**Candidate next steps** (ideas, not committed):
- Auto-fetch instruction PDFs by set number instead of manual upload.
- A pre-sorted-zones fallback mode if dense-pile accuracy stays hard.
- Auto-advance the guide when `check_piece` confirms the last piece of a step
  (still needs a builder utterance to speak — the agent can't start a turn).
- A "Powered by Gradium" badge if shown publicly.
