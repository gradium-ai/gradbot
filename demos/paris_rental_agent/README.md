# Paris Rental Agent

An open-source demo of a voice-first apartment search assistant for Paris.
Users describe what they want, the app turns that conversation into a structured
search profile, and confirmed profiles can be searched against live web results
from Tavily.

This is a demo application, not a production rental platform. It is useful as a
reference for Gradbot voice sessions, FastAPI auth, account-scoped tools, and a
small Render deployment.

## Features

- Voice and text onboarding for a renter search profile.
- Editable draft profile with required-field validation before search.
- Tavily-backed Paris listing discovery.
- Scoring against budget, rooms, furnished preference, commute, and nearby
  requirements.
- Saved/rejected listings and viewing-request drafts.
- Shared business logic for REST chat, voice tools, workers, and tests.
- Render Blueprint for web, worker, cron, and Postgres.

## Architecture

```text
demos/paris_rental_agent/
  main.py                  FastAPI entrypoint
  app/
    routes/                REST and WebSocket routes
    services/              Search, scoring, extraction, drafting
    voice/                 Gradbot voice session and tool definitions
    jobs/                  Scheduled search job
  static/                  Single-page frontend
  render.yaml              Render Blueprint
  tests/                   Focused API and service tests
```

## Quick Start

Run from the repository root.

```bash
uv pip install -r demos/paris_rental_agent/requirements.txt
cp demos/paris_rental_agent/.env.example demos/paris_rental_agent/.env
python demos/paris_rental_agent/scripts/setup_local.py
uvicorn demos.paris_rental_agent.main:app --reload --port 8000
```

Open `http://localhost:8000`.

The app falls back to SQLite when `DATABASE_URL` is unset. To use local
Postgres:

```bash
docker compose -f demos/paris_rental_agent/docker-compose.yml up -d db
```

Then set `DATABASE_URL` in `.env`.

## Configuration

Environment variables are preferred for deployed environments. Local
`config.yaml`, `.env`, SQLite databases, virtualenvs, and cache files are
ignored by git.

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | No | Defaults to local SQLite. Use `postgresql+psycopg://...` for Postgres. |
| `SECRET_KEY` | Production | JWT signing secret. Must be at least 32 bytes and not `change-me` in production. |
| `APP_ENV` | No | Set to `production` to enable secure cookies and runtime safety checks. |
| `BASE_URL` | Deploys | Public app URL, for example the Render web service URL. |
| `TAVILY_API_KEY` | Live search | Required for real listing search. Without it, search returns HTTP 503. |
| `GRADIUM_API_KEY` | Voice | Required for Gradbot voice. |
| `GOOGLE_MAPS_API_KEY` | No | Enables verified commute calculations. |
| `ENABLE_DEMO_ACCOUNT` | No | Defaults to `false`. Set to `true` only for local demos. |

For local voice/search config, copy:

```bash
cp demos/paris_rental_agent/config.example.yaml demos/paris_rental_agent/config.yaml
```

Do not commit `config.yaml` or `.env`.

## Demo Account

The fixed demo account is disabled by default. To enable it locally:

```bash
ENABLE_DEMO_ACCOUNT=true python demos/paris_rental_agent/scripts/setup_local.py
ENABLE_DEMO_ACCOUNT=true uvicorn demos.paris_rental_agent.main:app --reload --port 8000
```

When disabled, `/api/demo-credentials` returns 404 and startup does not create
the demo user.

## Render Deployment

The Blueprint is at:

```text
demos/paris_rental_agent/render.yaml
```

In Render:

1. Create **New > Blueprint**.
2. Connect the repo and branch.
3. Set **Blueprint Path** to `demos/paris_rental_agent/render.yaml`.
4. Fill `BASE_URL`, `TAVILY_API_KEY`, and `GRADIUM_API_KEY`.
5. Deploy and check `/healthz`.

The Blueprint creates:

- `paris-rental-agent`: FastAPI web service.
- `paris-rental-worker`: polling worker for saved searches.
- `paris-rental-scheduled-search`: daily 08:00 UTC cron job.
- `paris-rental-db`: Render Postgres database.

More details are in `DEPLOY_RENDER.md`.

## Security Notes

- Demo credentials are opt-in and should stay disabled in production.
- Production startup fails if `SECRET_KEY` is unset, too short, or left as
  `change-me`.
- Auth uses HTTP-only cookies; secure cookies are enabled when
  `APP_ENV=production`.
- Voice WebSocket auth uses the same-origin session cookie in the browser.
  Query-token fallback remains for non-browser clients, and issued voice tokens
  expire after five minutes.
- External listing links are restricted to `http` and `https`, escaped before
  rendering, and opened with `rel="noopener noreferrer"`.
- Render/Postgres URLs are logged with passwords masked.
- Cost-sensitive search sizes are capped at 50 results per request.

## Tests

```bash
cd demos/paris_rental_agent
uv sync --dev
uv run pytest tests
```

The test suite covers extraction, auth, profile intake, search gating,
normalization, scoring, saved/rejected listings, viewing drafts, and per-user
data isolation.

## Current Limitations

- Commute status is `unknown` unless `GOOGLE_MAPS_API_KEY` is configured.
- The text assistant uses deterministic intent matching; the voice path uses
  Gradbot tool-calling.
- Viewing messages are drafted only. Sending, dossier upload, payments, and
  browser automation are not implemented.
- Database migrations are not included; tables are created with SQLAlchemy
  metadata on startup.
