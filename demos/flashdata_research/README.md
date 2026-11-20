# Voice research with FlashData and Gradbot

![FlashData](static/logo.svg)

Ask a voice assistant to find web evidence, compare news sources, discover local businesses and retrieve existing YouTube transcripts. The agent uses two Gradbot tools to discover current FlashData MCP schemas and execute 14 queries; media delivery is a separate protected local workflow. One demo covers 15 capabilities, not 15 public marketplace listings.

## Requirements and setup

Use Python 3.12+, a stable Rust toolchain and [uv](https://docs.astral.sh/uv/). This repository builds Gradbot's native Python binding locally. From this directory:

```sh
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ in your browser. The voice interface reuses the repository's Simple Chat UI and bundled audio scripts, with a fixed English Skyler voice. It does not call a voice catalog merely to show the page. Echo cancellation starts enabled. Start/stop with the mic button; your browser may ask for microphone permission.

Before starting a call, provide the following through a protected launching environment or secret manager. Do not put real keys in Git, terminal history, the web page, chat or shared logs.

| Variable | Purpose |
| --- | --- |
| `FLASHDATA_API_KEY` | Your [FlashData API key](https://flashdata.dev/app/api-keys), sent only to MCP in `X-API-Key`; use source permissions limited to the queries you need. |
| `GRADIUM_API_KEY` | Your Gradium voice-services key. |
| `LLM_API_KEY` | Your own model provider key. |
| `LLM_MODEL` | A tool-capable model available to your account; choose explicitly. |
| `LLM_BASE_URL` | The OpenAI-compatible endpoint required by your selected model provider. |

The model and voice keys are passed through Gradbot's existing config loader. No FlashData key is included in model tool arguments. The demo refuses to open a voice session when required credentials/model are missing; debug errors are disabled. Do not enable verbose provider logging with real credentials or private data.

This is a local development demo, not an authenticated multi-user service. Bind to loopback as shown; do not expose the configured account's keys through a public deployment. No new backend, OAuth or cloud service is necessary to use the example locally.

## Costs and first query

Installation and the static page are free. Starting a conversation uses your model and Gradium services and may incur their fees. The UI asks you to acknowledge this before starting. **FlashData data requests consume your own Credits**; check [current pricing](https://flashdata.dev/pricing). Storage fees are separate.

Ask first: “Read FlashData prices; do not submit a data query.” The agent calls `flashdata_catalog`, then `flashdata_call` with `get_pricing`. That metadata read does not prove paid data retrieval. Voice/model fees still apply.

If you choose to spend Credits, approve one Google Search for battery recycling research, US English, 10 requested results. The query is `google_search` with `{"query":"battery recycling research","gl":"us","hl":"en","num":10}`. Ask for a short conclusion and the returned source links. Expected data includes `results[0].organic` entries with available `position`, `title`, `link` and `snippet`; missing fields and fewer results are possible.

The prompt asks the model to explain source/scope and obtain your approval before each paid query. This is a conversational control, not a hard budget cap; use a dedicated key and review your account usage. Tool selection follows the actual descriptions/schemas, without keyword routing. The MCP adapter submits once and does not replay timeouts or redirects. Model/voice request behavior remains Gradbot's native behavior. An interrupted request may have charged: check existing Jobs before resubmission. Preserve job IDs and `get_result` chunk metadata; do not repeat a paid query to retrieve another chunk.

## Fifteen capability workflows

Each task guide contains concrete input JSON, structured output fields, processing steps and limits. These are 15 capability mappings within one integration example, not 15 marketplace listings. The last workflow runs outside the voice session to protect storage credentials.

| Task and tool | Workflow and API documentation |
| --- | --- |
| [Google Search](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-search/README.md) — `google_search` | Search Google for battery recycling research in the US, in English. Make one request for 10 results and return a table of position, title, source URL and snippet. [API documentation](https://flashdata.dev/docs/sources/google_search?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_search-task). |
| [Google News](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-news/README.md) — `google_news` | Find Google News coverage of battery recycling. Make one request for 10 articles, group them by publisher, and preserve each original date label and article link. [API documentation](https://flashdata.dev/docs/sources/google_news?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_news-task). |
| [Google Images](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-images/README.md) — `google_images` | Find 20 Google Images results for solar panel diagrams. Keep images with a returned width of at least 1000 pixels, and list each image URL, source page and dimensions. [API documentation](https://flashdata.dev/docs/sources/google_images?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_images-task). |
| [Google Shopping](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-shopping/README.md) — `google_shopping` | Search Google Shopping once for wireless headphones in the US. Return 10 requested offers with product identifiers, merchant names, original price labels and source URLs. [API documentation](https://flashdata.dev/docs/sources/google_shopping?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_shopping-task). |
| [Google Video Search](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-video-search/README.md) — `google_videos` | Search Google Videos once for battery recycling process demonstrations. Request 10 results and group the returned titles, URLs and available durations by website. [API documentation](https://flashdata.dev/docs/sources/google_videos?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_videos-task). |
| [Google Places](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/google-places/README.md) — `google_places` | Search once for coffee shops in Singapore. Request 20 places, then make a shortlist from returned ratings of at least 4.2 and at least 20 reviews, keeping the addresses. [API documentation](https://flashdata.dev/docs/sources/google_places?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=google_places-task). |
| [YouTube Search](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-search/README.md) — `youtube_search` | Find 10 YouTube videos about battery recycling with one standard search. Return the video IDs, watch URLs, channels and available durations. [API documentation](https://flashdata.dev/docs/sources/youtube_search?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_search-task). |
| [YouTube Extended Search](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-extended-search/README.md) — `youtube_search_extended` | Run one extended YouTube search for battery recycling with 100 requested results. Count the returned videos by channel and state the actual sample size. [API documentation](https://flashdata.dev/docs/sources/youtube_search_extended?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_search_extended-task). |
| [YouTube Metadata](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-metadata/README.md) — `youtube_metadata` | Read metadata for YouTube video dQw4w9WgXcQ once, including chapters but excluding formats. Turn any returned chapter boundaries into timestamped navigation links. [API documentation](https://flashdata.dev/docs/sources/youtube_metadata?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_metadata-task). |
| [YouTube Transcript](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-transcript/README.md) — `youtube_transcript` | Retrieve the existing English automatic transcript for YouTube video dQw4w9WgXcQ once. Produce notes with timestamped citations; report a missing track without transcribing or switching tracks. [API documentation](https://flashdata.dev/docs/sources/youtube_transcript?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_transcript-task). |
| [YouTube Captions](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-captions/README.md) — `youtube_captions` | Retrieve the English uploader captions for YouTube video dQw4w9WgXcQ once, with auto=false. Convert the returned timed segments to SRT locally; report a missing track without substituting another. [API documentation](https://flashdata.dev/docs/sources/youtube_captions?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_captions-task). |
| [YouTube Creative Commons License Metadata](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-trainability/README.md) — `youtube_trainability` | Inspect the license-label classification for YouTube video dQw4w9WgXcQ with one request. Record the raw permitted field beside the video ID, without treating it as training authorization. [API documentation](https://flashdata.dev/docs/sources/youtube_trainability?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_trainability-task). |
| [YouTube Channel Data](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-channel/README.md) — `youtube_channel` | Read the @jawed channel profile and request 10 recent videos once. Save the channel ID and returned video IDs, titles and available view counts as a dated snapshot. [API documentation](https://flashdata.dev/docs/sources/youtube_channel?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_channel-task). |
| [YouTube Autocomplete](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-autocomplete/README.md) — `youtube_suggestions` | Get YouTube autocomplete suggestions for battery recycling in English and the US with one request. Return unique phrases while preserving the query and locale; do not estimate search volume. [API documentation](https://flashdata.dev/docs/sources/youtube_suggestions?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_suggestions-task). |
| [YouTube Download to Cloud Storage](https://github.com/rocosen/flashdata-mcp/blob/6f1c2b4888814995c332ef472ff21d890667af2e/npm/docs/youtube-cloud-storage/README.md) — `youtube_download` | Prepare a non-secret media delivery plan; the user submits once through the protected local CLI below. [API documentation](https://flashdata.dev/docs/sources/youtube_download?utm_source=gradium&utm_medium=integration&utm_campaign=intent_gtm_2026q4&utm_content=youtube_download-task). |

## Media delivery and functional limits

`youtube_download` and `submit_batch` are deliberately unavailable to the voice tool. A download requires the user's writable storage destination, scoped storage credentials, rights and byte-based cost approval. Have the agent prepare only a non-secret plan, then follow the [existing local download CLI guide](https://github.com/rocosen/flashdata-mcp/blob/main/npm/docs/youtube-cloud-storage/README.md). Use a protected file or stdin in your own environment, retain the job ID and inspect the final manifest there. This workflow needs Node.js 22.18+. Storage arguments are sent to FlashData for delivery; never speak them, paste them into chat, or let the agent read the populated secret file. Download job reads in this example return only ID/status, not the private manifest. Do not use `get_result` for unrelated cached private deliveries.

News provides article references/snippets, not guaranteed full text. Images are search references, not downloaded files. Shopping prices do not guarantee stock or checkout price; Places can omit contacts. Standard YouTube search requests at most 20 results; Extended requests at most 700 at a different rate, without guaranteeing counts. Transcript defaults to `auto=true`, Captions to `auto=false`; both retrieve existing tracks without ASR or translation. Trainability is a license-label signal, not a grant of training rights.

## Files and offline validation

- `main.py` is the FastAPI entry point and websocket wiring.
- `flashdata_agent.py` defines native Gradbot ToolDefs, session state/config and callbacks.
- `flashdata_client.py` reuses FlashData's MCP connection pattern, excludes downloads/batches and filters private download manifests. It does not return raw transport exceptions to the model.
- `prompts/main.txt` contains spending, citation and capability-limit instructions.
- `static/index.html` reuses Simple Chat's UI/audio protocol. FlashData supplies `static/logo.svg`.
- `tests/` uses real MCP protocol handling and native Gradbot configuration with in-memory HTTP fixtures and external sockets blocked.

```sh
uv sync --group dev
uv run pytest tests
```

On the validation machine (macOS 27, Apple ld 27037.1, Rust 1.96), the default release wheel built successfully but failed to load with `mis-aligned LINKEDIT string pool`. A local, unstripped development wheel loaded and passed the tests. If you encounter the same toolchain error, from `gradbot_py/` build a development wheel with `uv tool run maturin build --locked --strip false -i ../demos/flashdata_research/.venv/bin/python`, install the emitted wheel into the demo's environment with `uv pip install --python ../demos/flashdata_research/.venv/bin/python --reinstall --no-deps <emitted-wheel-path>`, then run the environment's Python directly. A subsequent `uv sync` may replace that development wheel. No upstream Rust code or global toolchain setting was changed for this workaround.

Tests cover all 14 query calls through the Gradbot callback, MCP discovery/header placement, invalid inputs, blocked tools, no paid-call replay, safe download job output, chunk metadata, native SessionConfig construction and static resources. These checks do not prove live voice quality, model behavior, paid data retrieval or storage delivery. No real model/voice/data calls or cloud deployment were performed for this contribution.

For the combined launcher, install from `demos/` and open `/flashdata_research/`; it discovers this `main.py` automatically. The demo's MCP dependency is also declared in the combined environment.

[Gradbot Python documentation](../../gradbot_py/README.md) · [Simple Chat reference](../simple_chat/README.md) · [FlashData support](https://github.com/rocosen/flashdata-mcp/issues) · [Privacy](https://flashdata.dev/legal/privacy) · [Terms](https://flashdata.dev/legal/terms). Source availability, official inclusion and a deployed service are separate states.
