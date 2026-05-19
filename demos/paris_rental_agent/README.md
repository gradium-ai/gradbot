# Paris Rental Agent: Voice enabled AI search using Gradbot and Tavily

A flagship demo for voice-enabled AI search. Users describe what they want in a
Paris apartment, Gradbot turns the conversation into a structured search
profile, and [Tavily](https://www.tavily.com/) searches the web for current
rental listings that can be normalized, scored, and reviewed in the app.

This is a demo application, not a production rental platform.

## What It Does

- Voice and text onboarding for a renter search profile.
- Editable draft profile with confirmation before search.
- Live web search for Paris listings through Tavily.
- Optional commute verification through Google Maps.
- Match scoring for budget, rooms, furnished preference, commute, and amenities.
- Saved/rejected listings and viewing-request drafts.
- Shared business logic across REST routes, voice tools, background jobs, and tests.

## Tavily Search

[Tavily](https://www.tavily.com/) powers the live listing discovery in this
demo. After the user confirms a search profile, the app converts the profile
into focused Paris rental queries, sends those queries to the
[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search),
and uses the returned web results to find relevant apartment listings.

The Tavily integration lives in `src/services/tavily_search.py`:

- `build_queries()` creates search queries from budget, bedrooms, rooms,
  furnished preference, and preferred arrondissements.
- `search_paris_rentals()` calls Tavily, deduplicates URLs, and returns raw web
  results.
- `src/services/search_pipeline.py` normalizes those results, scores them
  against the confirmed profile, and stores the matches.

To use Tavily locally, create an API key from Tavily, then set
`TAVILY_API_KEY` in `.env`. The
[Tavily API docs](https://docs.tavily.com/documentation/api-reference/introduction)
cover authentication, the API base URL, and available endpoints.

```bash
TAVILY_API_KEY=tvly-your-key
```

You can also put the key in `config.yaml`:

```yaml
tavily:
  api_key: "tvly-your-key"
```

Without a Tavily key, the app still runs, but live search returns a
configuration error.

## Run Locally

Run these commands from the repository root:

```bash
cd demos/paris_rental_agent
uv sync --dev
cp .env.example .env
uv run uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`.

The app uses a local SQLite database when `DATABASE_URL` is unset. Tables are
created automatically on startup.

To enable the fixed local demo account:

```bash
ENABLE_DEMO_ACCOUNT=true uv run python scripts/setup_local.py
ENABLE_DEMO_ACCOUNT=true uv run uvicorn main:app --reload --port 8000
```

Otherwise, create an account through the signup screen.

## Configuration

Copy `.env.example` to `.env` and fill only the integrations you need.

| Variable | Required | Notes |
| --- | --- | --- |
| `SECRET_KEY` | Production | JWT signing secret. Use a strong value outside local development. |
| `DATABASE_URL` | No | Defaults to local SQLite. Use `postgresql+psycopg://...` for Postgres. |
| `TAVILY_API_KEY` | Search | Required for live apartment search. |
| `GRADIUM_API_KEY` | Voice | Required for Gradbot voice sessions. |
| `GOOGLE_MAPS_API_KEY` | No | Enables verified commute calculations. |
| `APP_ENV` | No | Defaults to `development`. |
| `BASE_URL` | No | Defaults to `http://localhost:8000`. |
| `ENABLE_DEMO_ACCOUNT` | No | Defaults to `false`. Set to `true` only for local demos. |

For local voice/provider configuration, you can also copy:

```bash
cp config.example.yaml config.yaml
```

`config.yaml`, `.env`, local databases, virtualenvs, and cache files are ignored
by git.

## Optional Postgres

SQLite is enough for local testing. To use Postgres instead:

```bash
docker compose up -d db
```

Then set `DATABASE_URL` in `.env`.

## How It Is Built

- `main.py` creates the FastAPI app, initializes the database, mounts REST
  routes, mounts the voice WebSocket route, and serves the static frontend.
- `static/app.js` is a small single-page frontend for signup/login, onboarding,
  search results, saved listings, viewing drafts, text chat, and voice chat.
- `src/routes/` contains the HTTP API for auth, intake, profiles, search,
  listings, assistant chat, and voice.
- `src/voice/gradbot_session.py` defines the Gradbot voice prompt, tools, and
  WebSocket tool dispatch.
- `src/services/assistant_tools.py` is the shared application layer used by REST
  chat, voice tools, tests, and jobs.
- `src/services/search_pipeline.py` runs live search, normalizes results, scores
  listings, stores matches, and marks stale results after profile changes.
- `src/services/requirement_extraction.py`, `normalize.py`, `scoring.py`,
  `commute.py`, and `drafting.py` keep domain logic isolated and testable.
- SQLAlchemy models live in `src/models.py`; API schemas live in `src/schemas.py`.

## Tests

```bash
uv run pytest tests
```

The test suite covers requirement extraction, profile intake, auth, search
gating, normalization, scoring, saved/rejected listings, viewing drafts, and
per-user data isolation.

## Current Limitations

- Search requires `TAVILY_API_KEY`; without it, search endpoints return a
  configuration error.
- Commute status is unknown unless `GOOGLE_MAPS_API_KEY` is configured.
- Viewing messages are drafted only. Sending, dossier upload, payments, and
  browser automation are not implemented.
- Database migrations are not included; SQLAlchemy metadata creates tables on
  startup.
