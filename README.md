# Hotel Aurora · AI Reception Alpha

An alpha of an AI hotel receptionist that takes reservations by **chat and voice**, in **English and Polish**. The assistant talks to guests, checks real availability, quotes VAT-inclusive prices, places a temporary hold, confirms the booking and mirrors it into a per-room Google Calendar.

> **Alpha status.** This is a working demonstration, not a production PMS. There is no payment processing, no channel manager and no hardening against hostile internet traffic yet. See [Known limitations](#known-limitations).

---

## What it does

- **Chat concierge** at `/chat/` with a bilingual conversation UI.
- **Voice concierge** at `/voice/` using the browser Web Speech API (Chrome/Edge), with optional server-side OpenAI Whisper/TTS.
- **Real availability and pricing** from Django models, not model guesswork.
- **Booking lifecycle**: pending hold (30 minutes by default) → explicit guest confirmation → confirmed booking.
- **Booking lookup** at `/booking/` so a guest can find, confirm or cancel a reservation.
- **Per-room calendar sync** through a provider interface: a deterministic fake adapter for demos and a Google Calendar adapter for real calendars.
- **Provider-agnostic LLM layer**: OpenAI, Anthropic, Ollama/self-hosted, or the bundled offline **Demo policy** that needs no API key.
- **Django admin** for rooms, pricing rules, clients, bookings, providers, prompts and calendars.
- **English/Polish UI** with a language switcher and compiled Polish gettext catalogue.
- **Dockerised deployment** with SQLite on a named volume, WhiteNoise static files and gunicorn.

---

## Quick start (local, Windows PowerShell)

Requirements: Python 3.12+ (3.15 was used locally), Git.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Copy-Item .env.example .env
# Edit .env: set DJANGO_SETTINGS_MODULE=config.settings.dev,
# DJANGO_DEBUG=true and choose a local superuser password.

.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py bootstrap_superuser
.\.venv\Scripts\python.exe manage.py runserver
```

Open <http://127.0.0.1:8000/>.

The repository's local `.env` (gitignored) already contains a demo superuser:

| Username | Password |
| --- | --- |
| `admin` | `AuroraAdmin!2026` |

These credentials are **local demo only**. Change `DJANGO_SUPERUSER_PASSWORD` and `DJANGO_SECRET_KEY` before exposing the app anywhere.

### URLs

| Path | Purpose |
| --- | --- |
| `/` | Landing page, availability search and room types |
| `/chat/` | Chat concierge |
| `/voice/` | Voice concierge |
| `/booking/` | Look up a reservation by reference |
| `/booking/<REFERENCE>/` | Reservation detail, confirm/cancel |
| `/admin/` | Django admin |
| `/api/v1/` | REST API (see [docs/API.md](docs/API.md)) |
| `/healthz` | Liveness + database check |

---

## Docker

```powershell
Copy-Item .env.example .env
# Edit .env: set a real DJANGO_SECRET_KEY and superuser password.
docker compose up --build
```

Then open <http://localhost:8000/>.

What the container does on boot (`docker/entrypoint.sh`):

1. applies migrations;
2. collects static files for WhiteNoise;
3. seeds demo data when `SEED_DEMO_DATA=true` (idempotent);
4. creates the superuser from `DJANGO_SUPERUSER_*`;
5. starts gunicorn on port 8000.

SQLite lives in the `hotel_data` named volume at `/app/data/db.sqlite3`. The compose file forces `config.settings.prod`; the `.env` file is still used for branding, provider keys and credentials.

To reset the demo database:

```powershell
docker compose down -v
docker compose up --build
```

---

## AI providers

Providers are records in **Admin → AI providers**. The seed command creates four:

| Provider | Default model | Needs |
| --- | --- | --- |
| `demo` | `demo-policy-v1` | Nothing. Deterministic bilingual policy for offline demos and tests. |
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `anthropic` | `claude-3-5-haiku-latest` | `ANTHROPIC_API_KEY` |
| `ollama` | `llama3.1` | Reachable `OLLAMA_BASE_URL` and the provider enabled |

The first active default is used for new conversations. To switch, edit a provider in the admin and tick **default provider**; the model saves exclusively.

The agent is provider-agnostic: `apps/providers/` converts the internal `ChatMessage` / `ToolCall` / `LLMResponse` shapes to each vendor's API. The same tool loop powers chat and voice.

### Demo policy

With no API keys the seeded `demo` provider is the default. It understands English and Polish, extracts dates, guest counts, names, emails and phone numbers, calls the real tools, and walks through availability → hold → confirmation. This keeps the alpha fully demonstrable offline.

---

## Voice

The default speech stack is the **browser Web Speech API**:

- Speech-to-text: `SpeechRecognition` (`en-US` / `pl-PL`).
- Text-to-speech: `speechSynthesis`.
- No audio leaves the browser; the transcript is sent to the same chat API as typed messages.

Chrome and Edge are recommended. If a browser does not support speech recognition, the page falls back to chat.

For server-side speech, set `voice_stt_provider` / `voice_tts_provider` to `openai` in **Admin → Agent settings** and provide `OPENAI_API_KEY`. The endpoints `/api/v1/voice/transcribe/` and `/api/v1/voice/speak/` are already implemented.

---

## Google Calendar

The calendar layer is **one calendar per room**:

- `CalendarConnection` holds the provider and credentials.
- `RoomCalendar` maps one room to one Google calendar id.
- `BookingCalendarEvent` records the sync result and event id.

The demo seeds a `fake` connection, so confirmation and cancellation work without Google credentials.

To connect Google Calendar:

1. Create a Google Cloud project, enable the Calendar API and download a service-account key or OAuth client secret.
2. In the admin, create a **Calendar connection** with provider `Google Calendar` and paste the credentials JSON (or set `GOOGLE_CALENDAR_CREDENTIALS_FILE`).
3. Create one **Room calendar** per room and enter the calendar id shared with the service account.
4. For an authorised-user flow, run outside Docker:

   ```powershell
   .\.venv\Scripts\python.exe manage.py google_calendar_authorize --client-secret path\to\client_secret.json --output token.json
   ```

   Paste the token JSON into the connection's credentials field.

Confirmed bookings are pushed on commit (`transaction.on_commit`), so a calendar outage can never roll back a reservation. Failures are stored with `status=failed` and can be retried:

```powershell
.\.venv\Scripts\python.exe manage.py sync_calendars --retry-failed
.\.venv\Scripts\python.exe manage.py sync_calendars --reference HA-7KQ2M4
```

---

## Development

```powershell
# Tests (in-memory SQLite, no throttling)
.\.venv\Scripts\python.exe -m pytest

# Django checks
.\.venv\Scripts\python.exe manage.py check

# Lint
.\.venv\Scripts\python.exe -m ruff check .

# Refresh Polish translations after changing templates/Python strings
.\.venv\Scripts\python.exe scripts/update_translations.py
.\.venv\Scripts\python.exe scripts/compile_translations.py
```

The translation scripts exist because GNU gettext is not guaranteed on Windows. `scripts/pl_translations.py` holds the Polish dictionary; `locale/pl/LC_MESSAGES/django.mo` is the compiled catalogue committed with the app.

### Useful management commands

| Command | Purpose |
| --- | --- |
| `seed_demo` | Idempotent demo rooms, pricing, providers, calendars and one confirmed booking |
| `bootstrap_superuser` | Create/promote the superuser from `DJANGO_SUPERUSER_*` |
| `expire_holds` | Release pending holds whose deadline passed (run from cron) |
| `sync_calendars` | Sync bookings, retry failures or sync one reference |
| `google_calendar_authorize` | Produce an OAuth token for an authorised user |

---

## Project layout

```text
apps/
  hotel/          rooms, clients, pricing rules, bookings, availability, booking service
  api/            DRF serializers, views, URLs and error envelope
  agents/         provider-agnostic conversation engine, tools, prompts, models
  providers/      OpenAI, Anthropic, Ollama and demo LLM connectors
  voice/          browser/OpenAI speech providers and voice client config
  calendar_sync/  per-room calendar models, fake/Google adapters, sync service
  web/            public Django templates and views
config/
  settings/       base, dev, prod and test settings
templates/web/    landing, chat, voice, booking pages
static/           dependency-free CSS and vanilla JS
tests/            pytest suite (domain, API, agent, calendar, web)
scripts/          gettext extraction/compilation helpers
docs/             architecture, API, roadmap, branching, release notes
```

---

## Known limitations

- SQLite and a single gunicorn container; not a multi-tenant or high-concurrency deployment.
- No payment, deposit, invoice or channel-manager integration.
- Public booking lookup uses the reference only; there is no guest authentication.
- Throttling is in-memory per process.
- The browser speech stack depends on vendor support and microphone permissions.
- Google Calendar sync is one-way (hotel → calendar); external calendar edits are not imported.
- Email/SMS confirmations are not sent yet.
- The demo policy is intentionally rule-based; use a real provider for open-ended conversation.

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [REST API](docs/API.md)
- [Roadmap](docs/ROADMAP.md)
- [Branching and releases](docs/BRANCHING.md)
- [Release notes v0.1.0](docs/releases/v0.1.0.md)
- [Project memory](docs/PROJECT_MEMORY.md)
- [Agent working notes](AGENTS.md)

## License

Proprietary alpha. All rights reserved.
