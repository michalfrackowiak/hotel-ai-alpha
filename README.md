# Hotel Aurora · AI Reception Alpha

An alpha of an AI hotel receptionist that takes reservations by **chat, voice or a direct web form**, in **Polish and English**. The assistant talks to guests, checks real availability, quotes VAT-inclusive prices, places a temporary hold, confirms the booking, sends a bilingual confirmation by email or SMS and mirrors it into a per-room Google Calendar.

> **Alpha status.** This is a working demonstration, not a production PMS. Payments and deposits are tracked manually (no direct card processing or automatic capture) and the app is not hardened against hostile internet traffic yet. See [Known limitations](#known-limitations).

---

## What it does

- **Chat concierge** at `/chat/` with a bilingual conversation UI.
- **Voice concierge** at `/voice/` using the browser Web Speech API (Chrome/Edge), with optional server-side OpenAI Whisper/TTS.
- **Direct booking** at `/book/`: search a stay, choose a rate plan and create a confirmed reservation without the chat.
- **Real availability and pricing** from Django models, not model guesswork.
- **Named rate plans**: refundable or non-refundable percentage/fixed plans, selectable in the web form and by the agent tools.
- **Booking lifecycle**: pending hold (30 minutes by default) → explicit guest confirmation → confirmed booking. The web form confirms immediately after the guest submits their details.
- **Guest self-service**: change dates, room type or occupancy, and cancel with a reason through the signed link. A configurable policy previews the fee before the guest commits.
- **Cancellation policy**: pending holds are free; confirmed bookings are free until `check_in - CANCELLATION_FREE_DAYS`; late cancellations charge the first `CANCELLATION_FEE_NIGHTS` nightly rates, capped at the total.
- **Append-only audit log**: every create, confirm, modify, cancel and expiry records the actor, channel and before/after state.
- **Operations dashboard** at `/ops/` for today's arrivals/departures, holds, failed calendar syncs, missing confirmation emails, email/SMS delivery health and recent booking events.
- **Rate calendar**: nightly rates and minimum-stay rules editable in a month grid in the admin.
- **Confirmation email** in the guest's language, with a text body, HTML body and an `.ics` calendar attachment. Failed sends retry with exponential backoff from the scheduler, and the `/ops/` dashboard tracks delivered, failed, bounced and complained messages.
- **Confirmation SMS** through a pluggable provider layer: the offline console provider by default, Twilio when `SMS_PROVIDER=twilio` and the credentials are set. Each send attempt is recorded, provider delivery receipts update the booking/status history, and inbound guest replies are stored and shown in `/ops/` and the admin.
- **Payment and deposit tracking (manual/admin-first)**: bookings track payment status, amount paid/due, method/reference/notes and a configurable deposit policy (hotel-level `DEPOSIT_POLICY`/`DEPOSIT_VALUE`, with optional per-rate-plan overrides). Guests see what is due/paid (including deposit/amount due now) on the booking page and in confirmations; staff can record payment changes via the admin, `record_payment`, or the staff-only booking payments API (append-only payment audit events). No direct card processing or automatic capture.
- **Signed booking links** so a reference alone cannot read or cancel a reservation; guests can request a fresh link with their reference and email/phone.
- **Booking lookup** at `/booking/` so a guest can find, confirm or cancel a reservation.
- **Guest account** at `/my-bookings/`: email/password registration with email verification, password reset/change, optional Google sign-in, and a transitional single-use magic-link fallback. Signed-in guests can review bookings linked to their verified account/email and manage profile settings; signed booking links and reference+email/phone lookup remain available.
- **Per-room calendar sync** through a provider interface: a deterministic fake adapter for demos and a Google Calendar adapter for real calendars.
- **Calendar reconciliation** (`reconcile_calendars`) that reports and repairs events deleted or edited outside the app.
- **Provider-agnostic LLM layer**: OpenAI, Anthropic, Ollama/self-hosted, or the bundled offline **Demo policy** that needs no API key. API keys are encrypted at rest.
- **Django admin** for rooms, pricing rules, clients, bookings, audit events, daily rates, providers, prompts and calendars.
- **English/Polish UI** with a language switcher and compiled Polish gettext catalogue.
- **Accessible guest pages**: the booking and voice pages are gated by an automated check (`tests/test_accessibility.py`) and audited with axe-core; labels, landmarks, heading order and live call status are covered.
- **Persistent throttling** through the database cache, shared by every gunicorn worker.
- **Dockerised deployment** with SQLite on a named volume, WhiteNoise static files, gunicorn and a hold-cleanup scheduler sidecar.

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
| `/book/` | Direct booking: choose a stay, rate plan and guest details |
| `/chat/` | Chat concierge |
| `/voice/` | Voice concierge |
| `/booking/` | Look up a reservation by reference and email/phone |
| `/booking/<REFERENCE>/?token=…` | Reservation detail, confirm/modify/cancel through a signed link |
| `/my-bookings/` | Guest account: sign in/register (email/password, optional Google, or magic-link fallback), view linked bookings, manage profile/settings |
| `/my-bookings/login/<TOKEN>/` | Single-use magic-link sign-in (expires after 30 minutes) |
| `/ops/` | Staff operations dashboard (sign in first) |
| `/admin/` | Django admin, including the rate calendar |
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

What the web container does on boot (`docker/entrypoint.sh`):

1. applies migrations;
2. creates the database cache table used by throttling;
3. collects static files for WhiteNoise;
4. seeds demo data when `SEED_DEMO_DATA=true` (idempotent);
5. creates the superuser from `DJANGO_SUPERUSER_*`;
6. starts the web process (Daphne ASGI on port 8000).

A second `scheduler` container waits for the web container to become healthy, then every `HOLD_CLEANUP_INTERVAL_SECONDS` (default 300) runs `expire_holds`, `reconcile_calendars --apply`, `send_booking_emails`, `send_booking_sms`, `sync_channel_feeds --apply`, `openline_prune --days $OPENLINE_WEBHOOK_RETENTION_DAYS` (default 30), and `prune_phone_data --apply --days $VOICE_PHONE_RETENTION_DAYS` (default 30). It shares the same SQLite volume. On a host without Docker, run the same commands from cron or a systemd timer:

```cron
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py expire_holds
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py reconcile_calendars --apply
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py send_booking_emails
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py send_booking_sms
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py sync_channel_feeds --apply
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py openline_prune --days 30
*/5 * * * * cd /srv/hotel && /srv/hotel/.venv/bin/python manage.py prune_phone_data --apply --days 30
```

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

API keys are encrypted at rest with Fernet. Set `FIELD_ENCRYPTION_KEY` to a long random value in production; when unset, a key is derived from `DJANGO_SECRET_KEY`. To rotate the key without downtime, set `FIELD_ENCRYPTION_KEY_OLD` to the current value, set the new `FIELD_ENCRYPTION_KEY`, then run:

```powershell
.\.venv\Scripts\python.exe manage.py rotate_provider_keys
```

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

After a booking is confirmed, the agent asks whether the guest wants the confirmation by email or SMS. It collects the matching contact detail if it is missing and calls `send_confirmation_email` or `send_confirmation_sms`. The conversation defaults to Polish and switches to English when the guest asks for English.

### Confirmation SMS

SMS delivery goes through `apps/hotel/sms_providers.py`:

| `SMS_PROVIDER` | Behaviour | Needs |
| --- | --- | --- |
| `console` (default) | Records the message in the log and `ConsoleSmsProvider.outbox`; works offline and in tests. | Nothing |
| `twilio` | Posts to the Twilio Messages API. | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |

Set `BOOKING_SMS_ENABLED=false` to turn SMS off entirely. Each booking stores `confirmation_sms_status`, `confirmation_sms_error`, `confirmation_sms_provider_id`, `confirmation_sms_attempts`, `confirmation_sms_last_attempt_at`, `confirmation_sms_next_retry_at` and `confirmation_sms_sent_at`; each attempt is also recorded as an `SmsDelivery` row and provider receipts can refine it to `delivered` or `undelivered`. Failed sends are retried with exponential backoff from the scheduler (`SMS_MAX_ATTEMPTS`, `SMS_RETRY_BASE_SECONDS`, `SMS_RETRY_MAX_SECONDS`), and `/ops/` tracks SMS delivery health, retries and problem deliveries. The admin and `/ops/` show SMS delivery history and inbound replies; the admin offers **Resend confirmation SMS**. SMS failures never roll back a reservation.

To receive delivery receipts and inbound replies, point the provider at `POST /api/v1/sms/webhooks/<provider>/` (for example `twilio`). If `SMS_WEBHOOK_SECRET` is set, the request must include the matching `X-Sms-Webhook-Secret` header. Twilio-shaped callbacks and generic JSON payloads are supported; unknown receipts are acknowledged without breaking the provider.

### OpenLine telephony integration (v0.11.0, feature-flagged)

The vendored **`django-openline`** app connects the project to an existing self-hosted [OpenLine](https://github.com/michalfrackowiak/openline) server, which turns an Android phone's SIM into a programmable phone line. v0.11.0 adds the control plane plus a feature-flagged phone voice bridge: OpenLine webhooks create/update `PhoneCall` domain records, and inbound calls can be attached to the ASGI media endpoint at `/ws/voice/openline/` (Twilio-format audio). The bridge reuses the same `AgentEngine`/booking tools as chat and browser voice; booking writes still go only through `apps/hotel/booking_service.py`.

- **Off by default.** Keep `VOICE_PHONE_ENABLED=false` and `AgentSettings.phone_enabled=false` until Phase 0 real-server validation and pilot hardening are complete.
- **Control plane:** vendored under `django-openline/`, registered as `django_openline`. Configure `OPENLINE_URL`, `OPENLINE_API_KEY`, `OPENLINE_WEBHOOK_SECRET` (plus optional `OPENLINE_DEFAULT_DEVICE_ID` / `OPENLINE_DEFAULT_STREAM_URL`) in `.env`. Point OpenLine webhooks at `${HOTEL_PUBLIC_URL}/integrations/openline/webhook/`.
- **Voice bridge:** ASGI endpoint `/ws/voice/openline/` served by Daphne; set `VOICE_PUBLIC_WSS_URL` (or `HOTEL_PUBLIC_URL` for derived WSS) and `OPENLINE_INCOMING_STREAM_MODE=webhook_attach`. Runtime needs Redis/Channels for ASGI; Docker compose includes Redis and runs Daphne.
- **Operational commands:** `python manage.py migrate`, `python manage.py openline_check`, `django_openline.services.place_call()` / `@on_event(...)`, `openline_prune` for webhook/call retention, `prune_phone_data --days N [--apply]` for dry-run redaction of old terminal PhoneCall PII (defaults to `VOICE_PHONE_RETENTION_DAYS`, default 30; legal review still pending).
- **Status:** real OpenLine server Phase 0 validation (webhook payloads/signatures, WS handshake/frame shapes, transfer capability, latency) is still pending before pilot.

---

## Payments and deposit policy

Payment handling is **manual/admin-first**: there is no direct card/acquirer integration, automatic capture, chargeback handling or accounting sync.

- **Deposit policy** is configured at the hotel level (`DEPOSIT_POLICY=none|percent|fixed`, `DEPOSIT_VALUE` in `.env` / settings) and can be overridden per rate plan in the admin (inherit hotel policy, no deposit, percentage of total, or fixed amount). `PERCENT` treats the value as a percentage of the booking total; `FIXED` treats it as an amount in the hotel currency. `apps/hotel/deposit.py` `deposit_summary()` is the single source of truth for deposit amount, amount due now and the guest-facing policy text.
- **Booking payment state** tracks `payment_status` (unpaid/partially paid/paid/refunded), `amount_paid`, `amount_due`, `payment_method`, `payment_reference`, `paid_at` and `payment_notes`. Guests see payment and deposit summaries on the signed booking page and in confirmation email/SMS; token-gated booking API responses expose the same fields.
- **Recording payments**: staff can update payment state in the admin, with `manage.py record_payment`, or via `POST /api/v1/bookings/<reference>/payments/` (authenticated `is_staff` only, no guest booking tokens). Actions: `recorded`, `adjusted`, `refunded`, `waived`. Each change goes through `apps.hotel.booking_service.record_booking_payment()` and appends an immutable `BookingEvent` (`payment_recorded`/`payment_adjusted`/`payment_refunded`/`payment_waived`).
- **Operations visibility**: `/ops/` highlights active/pending bookings needing payment attention (unpaid or partially paid).

---

## Guest account

The web guest account at `/my-bookings/` gives guests a durable way to access their reservations without exposing a separate booking API credential.

- **Sign-in options**: email/password, optional Google sign-in, and a transitional single-use magic-link fallback.
- **Registration and verification**: new guest registrations are created inactive until the email verification link is confirmed. Email changes also require verification before the account email is switched.
- **Password flows**: guests can reset a forgotten password and change their password while signed in. Django password validators are enforced.
- **Profile/settings**: signed-in guests can update name, phone, preferred language and marketing consent; profile data is kept consistent with linked `Client` records where appropriate.
- **Booking visibility**: authenticated guests see bookings linked to their verified account/email(s), including account-linked bookings after verified email changes. The booking detail page issues the normal signed booking token for accessible bookings so existing confirm/modify/cancel flows continue to work.
- **Fallbacks preserved**: signed booking management links, reference + email/phone lookup, and magic-link sign-in remain supported. Phone-only bookings remain accessible through signed links and reference+phone lookup.
- **Security controls**: guest auth flows use neutral anti-enumeration responses, cache-backed throttling/cooldowns, and single-use hashed magic-link/email-verification tokens.

Google sign-in is optional. Configure an OAuth 2.0 Web client and set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`; add `${HOTEL_PUBLIC_URL}/my-bookings/google/callback/` as an authorised redirect URI. Guest throttling/verification settings are documented in `.env.example`.

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

Events edited or deleted directly in Google Calendar are found by the reconciliation command. It is a read-only dry run unless you pass `--apply`:

```powershell
.\.venv\Scripts\python.exe manage.py reconcile_calendars
.\.venv\Scripts\python.exe manage.py reconcile_calendars --apply --reference HA-7KQ2M4
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
| `expire_holds` | Release pending holds whose deadline passed (run from the scheduler/cron) |
| `send_booking_emails` | Retry confirmation emails that are due, honouring exponential backoff (`--force` resends everything) |
| `send_booking_sms` | Retry confirmation SMS messages that are due, honouring exponential backoff (`--force` resends sent messages) |
| `sync_calendars` | Sync bookings, retry failures or sync one reference |
| `reconcile_calendars` | Report calendar drift; `--apply` repairs it |
| `record_payment` | Record a staff payment change (recorded/adjusted/refunded/waived) and append a payment audit event (also available via the staff-only booking payments API) |
| `rotate_provider_keys` | Re-encrypt provider API keys after an encryption-key rotation |
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
- No direct card/acquirer payment processing, automatic capture, chargeback handling or accounting/invoice integration. Payments and deposit policy are tracked manually (admin, `record_payment`, or the staff-only payments API) with append-only audit events. (iCal channel manager is available; see `sync_channel_feeds`.)
- Guest account access supports email/password (with email verification, password reset/change), optional Google sign-in, and a transitional single-use magic-link fallback. Signed booking links and reference+email/phone lookup remain available. Guest auth flows use neutral anti-enumeration responses and cache-backed throttling; session security follows production cookie/TLS settings.
- Email delivery supports SMTP, SES and Postmark with provider-native bounce/complaint webhooks and scheduler-driven retry/backoff.
- The browser speech stack depends on vendor support and microphone permissions.
- Google Calendar sync is one-way (hotel → calendar); reconciliation repairs drift but does not import external bookings.
- SMS delivery ships with a console provider for offline demos and an optional Twilio adapter, with provider webhooks for delivery receipts and inbound replies, and scheduler-driven retry/backoff for failed or unsent confirmations. Reception can also resend from the admin.
- The demo policy is intentionally rule-based; use a real provider for open-ended conversation.

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [REST API](docs/API.md)
- [Roadmap](docs/ROADMAP.md)
- [Branching and releases](docs/BRANCHING.md)
- [Release notes v0.4.0](docs/releases/v0.4.0.md)
- [Release notes v0.3.0](docs/releases/v0.3.0.md)
- [Release notes v0.2.0](docs/releases/v0.2.0.md)
- [Release notes v0.1.0](docs/releases/v0.1.0.md)
- [Project memory](docs/PROJECT_MEMORY.md)
- [Backup and restore](docs/BACKUP.md)
- [Agent working notes](AGENTS.md)

## License

Proprietary alpha. All rights reserved.
