# Project memory

Living notes for whoever picks this up next. Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md), endpoints in [API.md](API.md), plans in [ROADMAP.md](ROADMAP.md) and the branch/release process in [BRANCHING.md](BRANCHING.md). This file records state, decisions and traps that those documents do not.

**Last updated:** 13 November 2026 · **Version:** v0.2.0 · **Tests:** 98 passing

## Current state

- Phases 0–8 are complete. The app books a room end-to-end by chat and voice in English and Polish, emails a bilingual confirmation and keeps per-room calendars in sync.
- Local database `data/db.sqlite3` is migrated and seeded. The local superuser is `admin` / `AuroraAdmin!2026` (demo only, `.env` is gitignored).
- Git is initialised with `main` and `develop`; `v0.1.0` is tagged and pushed to `github.com/michalfrackowiak/hotel-ai-alpha`. The `v0.2.0` release branch is `feature/v0.2.0-operational-confidence`.
- The default AI provider is the offline `demo` policy. No external API key is required to run or demo the app.
- The demo calendar connection uses the `fake` provider, so confirmation and cancellation exercise the real sync path without Google credentials.
- Booking links are signed and token-gated. The web lookup requires reference + email/phone; the API returns `manage_token` and `manage_url` on creation.
- Throttling uses the database cache in production (created by the entrypoint) and an in-memory cache in tests.
- The Docker compose stack includes a `scheduler` sidecar that runs `expire_holds` every five minutes.

## Decisions and why

| Decision | Reason |
| --- | --- |
| Django 5.2 + DRF, SQLite | Smallest stack that still gives admin, migrations, i18n, API and a credible production shape for an alpha. |
| Split settings (`base`/`dev`/`prod`/`test`) | Docker defaults to `prod`; tests use in-memory SQLite, fast hashing and no throttling. |
| Plain Django templates + vanilla JS | No Node build step, no dependency drift, easy to hand over. |
| `apps.providers` is not a Django app | It has no models or migrations; provider configuration lives in `agents.AIProvider`. |
| Provider-agnostic shapes (`ChatMessage`, `ToolCall`, `LLMResponse`) | The tool loop, prompts and UI are independent of any vendor. |
| Demo policy is the fallback provider | A missing key must not break the reception desk or the test suite. |
| Booking hold → explicit confirm | Prevents the assistant from committing a reservation the guest has not accepted. |
| Calendar sync on `transaction.on_commit` | A calendar outage must never roll back a reservation. |
| Calendar app named `calendar_sync` | Avoids shadowing Python's stdlib `calendar` module. |
| Gross/VAT-inclusive pricing | Matches how Polish hotel rates are quoted; tax is an informational split. |
| Custom gettext scripts instead of GNU gettext | Windows developers are not guaranteed to have `msgfmt`/`xgettext`. |
| Rule-based demo policy | Deterministic tests and offline demos; a live LLM is optional. |
| SQLite + one gunicorn container | Deliberate alpha scope; persistent throttling and hold cleanup are handled by the database cache and scheduler sidecar. |
| Signed `TimestampSigner` booking tokens | A short reference is guessable; a signed token containing a random per-booking secret makes links unguessable and revocable. |
| Contact lookup issues fresh links | Guests lose emails; reference + email/phone is enough to re-issue a link without storing a password. |
| Fernet encryption for provider keys | Stops API keys appearing in plaintext in SQLite backups or the admin database dump; rotation is a management command. |
| Database cache for throttling | No Redis dependency for the pilot; the cache table survives restarts and is shared by gunicorn workers. |
| Email after commit, with an idempotency marker | A mail outage must not roll back a booking, and retries must not spam the guest. |
| Reconciliation compares payload fields | Event-id-only checks miss edits made in Google Calendar; summary/location/dates/status catch real drift. |

## Runbook

All commands assume the repository root and the local virtualenv.

```powershell
# Setup
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createcachetable
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py bootstrap_superuser
.\.venv\Scripts\python.exe manage.py runserver

# Gate
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe -m ruff check .

# Static files (prod / Docker)
.\.venv\Scripts\python.exe manage.py collectstatic --noinput

# Translations after changing UI strings
.\.venv\Scripts\python.exe scripts/update_translations.py
.\.venv\Scripts\python.exe scripts/compile_translations.py

# Docker
docker compose up --build
docker compose down -v   # reset the demo database

# Operations
.\.venv\Scripts\python.exe manage.py expire_holds
.\.venv\Scripts\python.exe manage.py send_booking_emails
.\.venv\Scripts\python.exe manage.py sync_calendars --retry-failed
.\.venv\Scripts\python.exe manage.py sync_calendars --reference HA-XXXXXX
.\.venv\Scripts\python.exe manage.py reconcile_calendars
.\.venv\Scripts\python.exe manage.py reconcile_calendars --apply
.\.venv\Scripts\python.exe manage.py rotate_provider_keys
```

Health: `/healthz` and `/api/v1/health/`. Admin: `/admin/`. API root: `/api/v1/`.

## Traps and invariants

- **One write path.** `apps.hotel.booking_service` owns create/confirm/cancel/expire. Do not write bookings from views, tools or admin actions directly.
- **Availability is per physical room**, not per room type. Two holds for the same room with overlapping nights must be rejected.
- **Pending holds block only until `hold_expires_at`.** Null or future deadlines block; past deadlines do not. `expire_holds` runs in the compose `scheduler` sidecar (or host cron).
- **Calendar sync and confirmation email must stay after commit.** Calling either inside the transaction reintroduces rollback coupling and can email a guest about a booking that then rolls back.
- **Pending bookings are skipped by `sync_booking` before any event record is created.** This is intentional and tested.
- **A booking reference is not a secret.** Every guest-facing read/confirm/cancel path must go through `apps.hotel.access`; the API accepts `token`, `X-Booking-Token` or Bearer.
- **Rotating `Booking.access_token` revokes every link for that booking.** Do not log the raw token.
- **Provider keys are encrypted in `AIProvider.api_key`.** Reading the raw column shows `enc:v1:...`; never write plaintext directly with SQL. Use the model or the admin form.
- **Changing `FIELD_ENCRYPTION_KEY` requires `rotate_provider_keys`.** Set `FIELD_ENCRYPTION_KEY_OLD` first; otherwise existing keys cannot be decrypted.
- **The fake calendar provider has a class-level event store.** Tests must reset it; `tests/conftest.py` does this with an autouse fixture. Never assert across tests without a reset.
- **The fake calendar store is per process.** In Docker (gunicorn workers + a separate management-command process) `reconcile_calendars` reports every fake event as missing, because the store is not shared. That is expected for the fake provider; reconciliation against Google Calendar reads the real API. Do not run `reconcile_calendars --apply` against the fake provider to "fix" a Docker demo — it only recreates events in the command's own process.
- **Throttle state lives in the cache.** Production uses the database cache table created by the entrypoint; if the table is missing, requests fail with a cache error.
- **The demo provider has no `language` tool argument.** Language is carried by the system prompt and conversation; adding vendor-specific arguments breaks the demo policy.
- **`AgentSettings` is a singleton at `pk=1`.** `load()` creates it; `save()` forces the pk.
- **`AIProvider.save()` enforces a single default.** Marking one default clears the others; the registry reads the first active default.
- **Admin model forms use explicit `fields`.** Ruff's `DJ007` rule rejects `fields = "__all__"`; keep the field lists in sync with the models.
- **Polish strings are extracted from templates (`.html` and `.txt`) and Python**, then filled from `scripts/pl_translations.py`. Edit the dictionary and re-run both scripts; do not hand-edit only the `.mo`.
- **Translation scripts import `pl_translations` as a sibling module.** Run them by path from the repo root, as shown above.
- **The test settings must keep throttling off, use the fake calendar and use locmem email.** Otherwise API tests become flaky, make network calls or write to real inboxes.
- **The browser speech stack is vendor-dependent.** Chrome/Edge are the supported demo browsers; the voice page falls back to chat.
- **`.env` is not committed.** Keep provider keys, superuser credentials and Google credentials out of git.
- **Ruff ignores `E501` but enforces import order, unused imports and Django rules.** Run it before pushing; CI does not currently run ruff, so this is on the developer.

## Verification log

| Date | Version | Check | Result |
| --- | --- | --- | --- |
| 2026-11-13 | v0.1.0 | `pytest` | 62 passed |
| 2026-11-13 | v0.1.0 | `manage.py check` | 0 issues |
| 2026-11-13 | v0.1.0 | `ruff check .` | clean after import/field fixes |
| 2026-11-13 | v0.1.0 | Docker compose build + `/healthz` | image built; `/healthz` returns `{"status": "ok"}` |
| 2026-11-13 | v0.1.0 | Browser booking EN/PL | chat flow confirmed two bookings; fake calendar events synced |
| 2026-11-13 | v0.1.0 | GitHub Actions CI | green on `main` and `develop` |
| 2026-11-13 | v0.2.0 | `pytest` | 98 passed |
| 2026-11-13 | v0.2.0 | `manage.py check` | 0 issues |
| 2026-11-13 | v0.2.0 | `makemigrations --check` | no changes detected |
| 2026-11-13 | v0.2.0 | `migrate` | `agents.0002`, `hotel.0002` applied |
| 2026-11-13 | v0.2.0 | `ruff check .` | clean |
| 2026-11-13 | v0.2.0 | Docker `docker compose up --build -d` | image built; `web` healthy, `scheduler` started, `/healthz` returns `{"status": "ok"}` |
| 2026-11-13 | v0.2.0 | Docker scheduler sidecar | logs `Hold cleanup scheduler started (every 300s)` then `Expired 0 booking hold(s).` |
| 2026-11-13 | v0.2.0 | Docker API booking access | create → `403` without token → `200` with `token`; confirm with `X-Booking-Token`; calendar event `synced` |
| 2026-11-13 | v0.2.0 | Docker agent flow EN/PL (demo provider) | chat booked and confirmed `HA-ZGS23D` (EN) and `HA-9F4ARS` (PL); confirmation email with `.ics` sent in both languages |
| 2026-11-13 | v0.2.0 | Docker web lookup | reference + email returns `302` to a signed link; wrong contact returns `200` lookup page |
| 2026-11-13 | v0.2.0 | Docker email retry | first `send_booking_emails` sent 4 undelivered confirmations; second run sent 0 |
| 2026-11-13 | v0.2.0 | Polish page in Docker | `/` with `Accept-Language: pl` renders `Sprawdzam dostępność…` |
| 2026-11-13 | v0.2.0 | GitHub Actions CI | green on `main` and `develop` (tests + Docker build) |

Update this table when the release gate is re-run.

## Open questions for v0.3.0

- Cancellation policy: hotel-wide free-cancellation window, or per rate/pricing rule?
- Reservation modification: edit in place, or cancel + create a new hold so availability logic stays untouched?
- Audit log: a dedicated append-only table, or structured JSON logs with an admin viewer?
- Operations dashboard: a custom admin index or a separate staff-only page?
- Email delivery: keep SMTP, or add a transactional API with bounce handling?
- Reconciliation schedule: run `--apply` nightly from the scheduler sidecar, or keep it manual with alerts?

## Next move

Start v0.3.0 on `develop` from the `v0.2.0` tag. Recommended order:

1. Cancellation policy and reason capture (guest-facing value, small domain change).
2. Reservation modification through the signed link (reuses availability and pricing).
3. Audit log for every booking transition (needed before more write paths).
4. Operations dashboard (arrivals, holds, failed syncs, email status).
5. Nightly reconciliation and email bounce handling (operational hardening).
6. Update roadmap, this file and `docs/releases/v0.3.0.md` before tagging.
