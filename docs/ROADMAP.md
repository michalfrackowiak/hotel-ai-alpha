# Roadmap

Status as of **13 November 2026**.

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

The alpha release covering phases 0–7 is documented in [releases/v0.1.0.md](releases/v0.1.0.md). The operational-confidence release covering phase 8 is documented in [releases/v0.2.0.md](releases/v0.2.0.md).

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

## Next milestone — v0.3.0 (planning)

Theme: **guest self-service**. The pilot is now operationally safe; v0.3 should reduce reception workload and give guests more control.

### Must have

1. **Guest cancellation policy** — configurable free-cancellation window, cancellation reasons and a fee preview before the guest confirms.
2. **Reservation modification** — change dates or room type through the signed link, with availability and price re-checked in one transaction.
3. **Audit log** — append-only record of booking transitions (actor, channel, before/after state) visible in the admin.
4. **Structured operations dashboard** — today's arrivals/departures, pending holds, failed calendar syncs and email delivery status on one admin page.
5. **Rate calendar** — nightly rates and minimum-stay rules editable in a month view.

### Should have

- Nightly `reconcile_calendars --apply` in the scheduler and an alert on non-repairable drift.
- Email delivery provider integration (SES/Postmark) with bounce handling.
- Demo-policy coverage for more date formats (`12/15/2026`, `15.12.2026`, relative dates).
- Export of bookings and revenue to CSV.
- Docker smoke-test script and a documented backup/restore drill.

### Exit criteria

- A guest can change or cancel within policy without contacting reception.
- Every booking transition is attributable to a channel and a timestamp.
- Reception can start the day from one dashboard page.

### Release process

Follow [BRANCHING.md](BRANCHING.md): branch `feature/v0.3.0-*` from `develop`, keep CI green, update this roadmap, `docs/PROJECT_MEMORY.md`, release notes and the next-milestone plan before merging to `main` and tagging `v0.3.0`.
