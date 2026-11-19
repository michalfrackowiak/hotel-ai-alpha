# Roadmap

Status as of **19 November 2026**.

| Phase | Milestone | Status |
| --- | --- | --- |
| 0 | Foundations and delivery pipeline | ✅ Complete |
| 1 | Hotel domain and pricing | ✅ Complete |
| 2 | Availability, booking engine and REST API | ✅ Complete |
| 3 | AI receptionist and tool loop | ✅ Complete |
| 4 | Voice concierge | ✅ Complete |
| 5 | Per-room calendar sync | ✅ Complete |
| 6 | Admin, seed data and operations | ✅ Complete |
| 7 | Bilingual UI, hardening and release | ✅ Complete (v0.1.0) |
| 8 | Operational confidence | ✅ Complete (v0.2.0) |
| 9 | Guest self-service | ✅ Complete (v0.3.0) |
| 10 | Revenue and delivery hardening | ✅ Complete (v0.4.0) |
| 11 | Guest experience and polish | ✅ Complete (v0.5.0) |

The alpha release covering phases 0–7 is documented in [releases/v0.1.0.md](releases/v0.1.0.md). The operational-confidence release covering phase 8 is documented in [releases/v0.2.0.md](releases/v0.2.0.md). The guest self-service release covering phase 9 is documented in [releases/v0.3.0.md](releases/v0.3.0.md). The revenue-hardening release covering phase 10 is documented in [releases/v0.4.0.md](releases/v0.4.0.md); the guest-experience release covering phase 11 is in [releases/v0.5.0.md](releases/v0.5.0.md). Subsequent releases through **v0.7.0** are documented in [`docs/releases/`](releases/), including channel manager/currency/PWA slices, agent improvements (post-confirmation email offer, flexible natural-language date parsing), the guided seven-step booking scenario with Polish-first language handling and SMS confirmation delivery, the v0.6.10 demo-provider fix for a forgotten guest count, and operational-visibility work (email history, scheduler load coverage, SMS delivery receipts and inbound replies).

---

## Phase 0 — Foundations and delivery pipeline

**Goal:** a reproducible Django project that can be built and tested locally and in Docker.

- [x] Django 5.2 + DRF project with split settings (`base`, `dev`, `prod`, `test`)
- [x] SQLite database at `data/db.sqlite3`
- [x] App layout: `hotel`, `api`, `agents`, `providers`, `voice`, `calendar_sync`, `web`
- [x] `.env.example`, dotenv loading, env-driven branding and limits
- [x] Dockerfile, entrypoint, compose file, named volume, health check
- [x] GitHub Actions CI: Django checks, pytest, Docker build
- [x] `.gitignore`, `.dockerignore`, `.gitattributes`, `pyproject.toml`, ruff and pytest config

**Exit criteria:** `manage.py check` passes; the test suite runs; the Docker image builds and serves `/healthz`.

---

## Phase 1 — Hotel domain and pricing

**Goal:** model the hotel and price a stay deterministically.

- [x] `RoomType`, `Room`, `Client`, `PricingRule`, `Booking` models with admin
- [x] Human-friendly booking references (`HA-XXXXXX`)
- [x] Per-night pricing with seasonal, weekend, last-minute and long-stay rules
- [x] Priority ordering, percentage + fixed adjustments, floor at zero
- [x] Gross/VAT-inclusive totals with informational tax split
- [x] Stay validation: date order, past dates, max nights, advance window, capacity
- [x] Unit tests for pricing and validation

**Exit criteria:** a quote returns a stable nightly breakdown and total for any valid stay.

---

## Phase 2 — Availability, booking engine and REST API

**Goal:** search real availability and move a booking through hold → confirm → cancel.

- [x] Per-room overlap detection with hold expiry
- [x] Availability search across room types with quotes
- [x] Transactional booking service with row locking and client deduplication
- [x] Pending hold, confirmation, cancellation and stale-hold expiry
- [x] REST API: health, room types, availability, pricing, bookings
- [x] Consistent JSON error envelope and throttling
- [x] Booking lookup page and reservation detail page
- [x] Integration tests for the full booking lifecycle and double-booking rejection

**Exit criteria:** two guests cannot hold the same room for overlapping dates; the API lifecycle test is green.

---

## Phase 3 — AI receptionist and tool loop

**Goal:** a bilingual assistant that uses real hotel data instead of guessing.

- [x] Provider-agnostic `ChatMessage` / `ToolCall` / `LLMResponse` interface
- [x] OpenAI, Anthropic, Ollama and offline demo connectors
- [x] Provider registry, default-provider selection and demo fallback
- [x] `AgentSettings` singleton with EN/PL prompts, greetings, fallbacks and limits
- [x] Tool loop with `check_availability`, `get_pricing`, `create_booking`, `confirm_booking`, `get_booking`, `cancel_booking`
- [x] Conversation and message persistence, history window, tool trace
- [x] Chat API and chat UI with booking sidebar
- [x] English and Polish end-to-end agent tests against the demo policy

**Exit criteria:** the demo policy books and confirms a room in English and Polish using only tools.

---

## Phase 4 — Voice concierge

**Goal:** reserve a room by speaking, without a separate business-logic path.

- [x] Browser Web Speech API loop (STT + TTS) in Chrome/Edge
- [x] Server-side OpenAI Whisper/TTS providers behind the same interface
- [x] Voice client config endpoint with languages, greetings and modes
- [x] Voice page with call controls, transcript, status states and booking panel
- [x] Voice conversations reuse chat sessions, messages and tools
- [x] Graceful fallback to chat when speech recognition is unavailable

**Exit criteria:** a guest can complete the same booking flow by voice, and the transcript shows the real tool results.

---

## Phase 5 — Per-room calendar sync

**Goal:** mirror confirmed bookings into one calendar per room without risking reservations.

- [x] `CalendarConnection`, `RoomCalendar`, `BookingCalendarEvent` models
- [x] Fake provider for demos/tests and Google Calendar provider
- [x] One calendar per room with idempotent event records
- [x] Create/update on confirmation, delete on cancellation
- [x] Sync after commit so calendar failures cannot roll back bookings
- [x] Failure persistence and `sync_calendars --retry-failed`
- [x] Service-account and authorised-user OAuth support
- [x] `google_calendar_authorize` helper command
- [x] Tests for payload, pending skip, confirmation, cancellation and missing mapping

**Exit criteria:** a confirmed booking creates exactly one event in the mapped room calendar; cancelling deletes it.

---

## Phase 6 — Admin, seed data and operations

**Goal:** make the alpha operable by a hotel team.

- [x] Django admin for rooms, clients, pricing, bookings, providers, prompts, conversations and calendars
- [x] Idempotent `seed_demo`: 2 room types, 5 rooms, 3 pricing rules, clients, providers, fake calendar, sample booking
- [x] `bootstrap_superuser` from environment variables
- [x] `expire_holds` command for scheduled hold cleanup
- [x] Calendar retry and single-reference sync commands
- [x] Health endpoint with database check
- [x] Docker entrypoint: migrate → collectstatic → seed → superuser → gunicorn
- [x] Seed and command tests

**Exit criteria:** a fresh container boots with usable demo data and an admin login.

---

## Phase 7 — Bilingual UI, hardening and release

**Goal:** a presentable v0.1.0 alpha in English and Polish.

- [x] Full Polish gettext catalogue (`locale/pl/LC_MESSAGES/django.po` / `.mo`)
- [x] Language switcher, `LocaleMiddleware`, translated templates and domain errors
- [x] Translation extraction/compilation scripts that work without GNU gettext
- [x] Responsive, dependency-free CSS and vanilla JS
- [x] Security settings for production (`prod.py`, WhiteNoise, optional TLS/HSTS)
- [x] README, architecture, API, branching, roadmap and release notes
- [x] 62 passing tests, `manage.py check`, ruff
- [x] Git repository, branch strategy, milestone tag and GitHub remote

**Exit criteria:** `pytest`, `manage.py check`, `ruff check`, `docker compose up --build` and a browser booking in both languages all succeed.

---

## Phase 8 — Operational confidence (v0.2.0)

**Goal:** make the alpha safe to run for a real pilot without a developer watching it.

- [x] Confirmation email in English and Polish with a text body, HTML body and `.ics` attachment
- [x] Idempotent delivery marker and `send_booking_emails` retry command
- [x] Signed booking-management links plus reference + contact lookup
- [x] API detail/confirm/cancel require a booking token (`token`, `X-Booking-Token` or Bearer)
- [x] `AIProvider.api_key` encrypted at rest with Fernet and a documented rotation command
- [x] Database-backed cache so DRF throttling survives restarts and multiple workers
- [x] Docker `scheduler` sidecar that runs `expire_holds` every five minutes
- [x] `reconcile_calendars` dry run and `--apply` repair for missing, modified, failed and stale events
- [x] Tests for email, access control, encryption/rotation, persistence and reconciliation
- [x] Updated API docs, README, architecture notes, release notes and Polish catalogue

**Exit criteria:** a pilot hotel can run a week of real enquiries with email confirmations and no manual hold cleanup; a booking cannot be read or cancelled with a guessed reference; calendar drift is detectable and repairable with one command.

---

## Phase 9 — Guest self-service (v0.3.0)

**Goal:** reduce reception workload and give guests control without weakening the booking invariants.

- [x] Configurable cancellation policy with a single preview used by the page, API, agent and write path
- [x] Cancellation reasons and notes recorded on the booking
- [x] Reservation modification (dates, room type, occupancy) through the signed link and API
- [x] Append-only `BookingEvent` audit log with actor, channel and before/after state
- [x] Staff operations dashboard at `/ops/` (arrivals, departures, holds, failed syncs, email gaps, recent events)
- [x] Rate calendar with nightly `DailyRate` overrides and minimum-stay rules
- [x] Admin month grid for daily rates and read-only audit views
- [x] Tests for policy windows, fees, modification, audit, ops access and rate-calendar pricing
- [x] Updated API docs, README, architecture notes, release notes and Polish catalogue

**Exit criteria:** a guest can change or cancel within policy without contacting reception; every booking transition is attributable to a channel and timestamp; reception can start the day from one dashboard page.

---

## Phase 10 — Revenue and delivery hardening (v0.4.0)

**Goal:** make pricing and operations less manual and close the remaining delivery gaps.

- [x] Rate plans and restrictions: closed-to-arrival, fully-closed, min/max length-of-stay plus named `RatePlan`s with percentage/fixed derived nightly rates
- [x] Nightly reconciliation and alerts: `reconcile_calendars --apply` runs from the scheduler sidecar; `/ops/` shows a calendar-drift counter
- [x] Email delivery provider: pluggable SMTP/SES/Postmark backends, delivery status tracking, bounce/complaint webhooks
- [x] Booking and revenue export: `manage.py export_bookings` writes CSV for a date range
- [x] Backup/restore drill: documented in [docs/BACKUP.md](BACKUP.md)
- [x] Demo-policy coverage for more date formats (`12/15/2026`, `15.12.2026`, relative dates)
- [x] Per-room housekeeping status on the operations dashboard
- [x] Guest-visible rate breakdown: nightly source shown on the booking page
- [x] Docker smoke-test script wired into CI
- [x] Updated API docs, README, architecture notes, release notes and Polish catalogue

**Exit criteria:** reception can change prices and restrictions without touching code or raw rows; calendar drift and email delivery failures are visible without reading logs; a backup can be restored into a clean container and verified with the test suite.

---

## Phase 11 — Guest experience and polish (v0.5.0)

**Goal:** let a guest complete the whole journey on the website, in English or Polish, without chat or voice — and make the guest-facing pages accessible.

### Released slices

- **v0.4.10** — passwordless guest account at `/my-bookings/` (magic-link login, booking list, session-based detail access).
- **v0.4.11** — public booking flow at `/book/` with named rate-plan selection, direct booking links from availability results and the room cards.
- **v0.4.12** — transactional email hardening: exponential retry/backoff, provider-native SES/Postmark bounce parsing, and an email delivery section on `/ops/`.
- **v0.5.0** — accessibility pass and milestone release.

### Delivered

- [x] Passwordless guest account with single-use magic links, neutral responses and cache-backed cooldowns
- [x] Public booking flow at `/book/` that quotes through `search_availability` and confirms through `create_booking()` / `confirm_booking()`
- [x] Named rate-plan selection in the web form, with per-plan totals from the availability engine
- [x] Transactional email retry with exponential backoff, provider message ids and a `/ops/` delivery dashboard
- [x] Provider-native SES (including SNS) and Postmark bounce/complaint/delivery webhooks
- [x] Accessibility pass on `/book/` and `/voice/`: labelled controls, a labelled rate-plan radiogroup, a live call-status region, decorative SVG handling and a sane heading order
- [x] Automated accessibility check in `tests/test_accessibility.py` for the booking and voice pages in English and Polish
- [x] axe-core audit: 0 violations on the booking (start and quote) and voice pages in both languages
- [x] Updated API docs, README, release notes, project memory and Polish catalogue

**Exit criteria:** a guest can book, change and cancel entirely from the website without chat; a guest can log in and see every reservation tied to their email; the voice and booking pages pass an automated accessibility check. **All met in v0.5.0.**

---

## Milestone v0.6.0 — released

Theme: **distribution and reach**. The guest journey is complete and accessible; the next milestone widens where bookings come from and how prices are shown.

### Must have

1. **Channel manager (iCal import/export)** — DONE in v0.5.1. OTA reservations are imported into per-room `RoomBlock` rows (no synthetic bookings); availability, creation and modification honour them, and `Booking.clean()` rejects overlaps. A signed `/calendar/<token>.ics` feed exports blocking bookings (pending holds as `TENTATIVE`) and manual blocks; conflicts with local blocking bookings are counted. The scheduler runs `sync_channel_feeds --apply`.
2. **Multi-currency display** (done in v0.5.2 slice) — a currency selector in the header sets a session/cookie; prices render with an indicative conversion next to the base-currency total via the `money` filter. Stored amounts stay in the hotel currency.
3. **Mobile PWA install prompt** (done in v0.5.2 slice) — `manifest.json`, service worker caching static assets and visited pages, and an install button shown on `beforeinstallprompt`.

### Should have

- [x] Colour-contrast audit of the guest pages and a manual screen-reader pass for the voice flow (automated token audit in `tests/test_accessibility.py` checks text, semantic badges, alpha text over dark surfaces, focus rings and control borders).
- [x] A per-message email history view rather than only the current delivery state (`EmailDelivery` model, admin inline, webhook sync).
- [x] Load-test the scheduler and retry queue with a large backlog (`tests/test_scheduler_load.py`).

### Exit criteria

- An imported iCal feed blocks the matching room without creating a duplicate local booking, and reconciliation reports drift in both directions.
- A guest can switch the displayed currency and the converted estimate is clearly marked as indicative.
- The app installs to a phone home screen and still renders the booking page offline-safe.

### Release process

Follow [BRANCHING.md](BRANCHING.md): branch `feature/v0.6.0-*` from `develop`, keep CI green, update this roadmap, `docs/PROJECT_MEMORY.md`, release notes and the next-milestone plan before merging to `main` and tagging `v0.6.0`.

---

## Milestone v0.6.9 — released

Theme: **the guided booking scenario**. The conversation now follows the reception script step by step, starts in Polish and offers English, and closes with a choice of email or SMS confirmation.

### Delivered

- [x] Seven-step scenario shared by chat and voice: welcome/language, arrival date, departure date or duration, guest count, availability and room selection, confirmation prompt, delivery choice
- [x] Default language Polish with dynamic switching to English mid-conversation
- [x] Pluggable SMS delivery: offline console provider by default, optional Twilio adapter
- [x] Booking SMS status fields, admin fieldset and **Resend confirmation SMS** action
- [x] `send_confirmation_sms` and `transfer_to_human` agent tools
- [x] Resort Neptun Jastrzębia Góra branding and updated `.env.example`
- [x] Migrations `agents.0003`/`0004` and `hotel.0014`, including a prompt-refresh data migration
- [x] 17 new Polish strings; catalogue at 617 extracted, all translated
- [x] Rewritten end-to-end demo tests for the sequential flow, SMS, Polish and handover

**Exit criteria:** a guest can book by chat or voice through the seven steps, in Polish or English, and receive the confirmation by email or SMS. **All met in v0.6.9.**

---

## Milestone v0.7.0 — ✅ Complete

Theme: **operational visibility**. Reception can trace confirmation delivery and guest replies, not just the latest status on a booking.

### Done

- [x] Email history: expose the append-only `EmailDelivery` rows in `/ops/` (last 20 attempts, newest first) with reference, recipient, subject, status, attempt and error
- [x] Read-only `EmailDelivery` admin list with a subject column; the booking inline shows the subject too
- [x] Tests for send/retry rows, webhook sync by message id or latest row, a webhook with no delivery row, ops display in EN/PL and admin read-only permissions
- [x] Polish catalogue updated for the new ops copy
- [x] Scheduler: load-test with a large backlog (`tests/test_scheduler_load.py` covers 200 expired holds and 100 pending confirmation emails)
- [x] SMS: per-attempt `SmsDelivery` history and provider delivery-receipt webhooks (`/api/v1/sms/webhooks/<provider>/`, `SMS_WEBHOOK_SECRET` / `X-Sms-Webhook-Secret`) that update booking and delivery status; inbound guest SMS replies stored as `InboundSmsMessage` records and matched to a booking by phone when possible; read-only SMS delivery/reply history in `/ops/` and the admin

**Exit criteria:** reception can trace every confirmation attempt and inbound SMS reply from `/ops/` or the admin, in both languages. **All met in v0.7.0.**

---

## Milestone v0.8.0 - complete on develop (unreleased)

Theme: **SMS retry/backoff parity with email**. Failed or unsent confirmation SMS messages are retried automatically with exponential backoff, with scheduler/command support and operational visibility. Merged to `develop` via PR #4 and PR #5 (CI green); no release cut.

### Delivered

- [x] Add SMS retry state on bookings (`confirmation_sms_next_retry_at`) and mirror email backoff settings (`SMS_MAX_ATTEMPTS`, `SMS_RETRY_BASE_SECONDS`, `SMS_RETRY_MAX_SECONDS`)
- [x] Schedule and clear SMS retries in `send_booking_confirmation_sms()`; add `send_pending_confirmation_sms()` and `send_booking_sms` management command
- [x] Run SMS retries from the scheduler sidecar alongside email retries
- [x] Show SMS retry queue/problem next-retry state in `/ops/` and expose retry fields in admin
- [x] Tests for SMS backoff, pending queue/cap, command behavior, and docs/translations updates
- [x] Scheduler load coverage for pending confirmation SMS backlog and due SMS retry backlog (`tests/test_scheduler_load.py`)

**Exit criteria:** failed or unsent confirmation SMS messages retry automatically on the same operational model as email, and reception can see retry state from `/ops/` and the admin.
