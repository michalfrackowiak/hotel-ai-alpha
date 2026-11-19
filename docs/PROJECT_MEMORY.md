# Project memory

Living notes for whoever picks this up next. Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md), endpoints in [API.md](API.md), plans in [ROADMAP.md](ROADMAP.md) and the branch/release process in [BRANCHING.md](BRANCHING.md). This file records state, decisions and traps that those documents do not.

**Last updated:** 19 November 2026 - **Version:** v0.7.0 - **Tests:** 223 passing

## Current state

- Phases 0–11 are complete. The app books a room end-to-end by chat and voice in Polish and English, emails or texts a confirmation and keeps per-room calendars in sync.
- The conversation follows a seven-step reception script shared by chat and voice: welcome/language, arrival date, departure date or duration, guest count, availability and room selection, confirmation prompt, then a choice of email or SMS delivery. Polish is the default; the engine switches to English when the guest asks.
- The demo hotel is branded **Resort Neptun Jastrzębia Góra**; the assistant is still called **Aurora**.
- Confirmation SMS is delivered through a pluggable provider layer: the offline `console` provider by default, or Twilio when `SMS_PROVIDER=twilio` and the credentials are set. Each send attempt is recorded as a `SmsDelivery` row; the booking stores the current SMS delivery state/attempt/retry metadata. Failed or unsent confirmations are retried with exponential backoff from the scheduler (`send_booking_sms`, `SMS_MAX_ATTEMPTS`, `SMS_RETRY_BASE_SECONDS`, `SMS_RETRY_MAX_SECONDS`), and `/ops/` shows SMS delivery health, retries and problem deliveries. Provider delivery-receipt webhooks (`POST /api/v1/sms/webhooks/<provider>/`, gated by `SMS_WEBHOOK_SECRET` / `X-Sms-Webhook-Secret` when configured) update the booking and matching/latest delivery, and inbound guest replies are stored as `InboundSmsMessage` records (linked to a booking by normalized phone when possible). Read-only SMS delivery history and inbound replies are shown in `/ops/` and the admin.
- Non-booking enquiries and explicit requests for a receptionist call `transfer_to_human`, which records a handover request in the conversation state.
- Guests can change or cancel a reservation through the signed link, with a policy fee preview and an audit trail.
- Reception has a staff operations dashboard at `/ops/` and a month-grid rate calendar in the admin.
- Local database `data/db.sqlite3` is migrated and seeded. The local superuser is `admin` / `AuroraAdmin!2026` (demo only, `.env` is gitignored).
- Git is initialised with `main` and `develop`; releases through `v0.6.10` are tagged and pushed to `github.com/michalfrackowiak/hotel-ai-alpha`.
- The default AI provider is the offline `demo` policy. No external API key is required to run or demo the app.
- The demo calendar connection uses the `fake` provider, so confirmation and cancellation exercise the real sync path without Google credentials.
- Booking links are signed and token-gated. The web lookup requires reference + email/phone; the API returns `manage_token` and `manage_url` on creation.
- Guests have a passwordless account at `/my-bookings/`: a single-use magic link (stored hashed, 30-minute expiry) signs them in and lists every booking tied to that email. A signed-in guest can open their bookings without a token.
- Guests can book directly at `/book/` without the chat: the page quotes through `search_availability`, lists the standard rate plus every applicable named `RatePlan`, creates the booking through `create_booking(source=web)` and confirms it immediately through `confirm_booking`.
- Availability results on the home page and the room cards link straight to `/book/` with the room type, dates and guest count prefilled.
- Confirmation email delivery is retried with exponential backoff. Each attempt updates `confirmation_email_attempts`, `confirmation_email_last_attempt_at` and `confirmation_email_next_retry_at`; the scheduler runs `send_booking_emails`, which only picks up bookings whose backoff window has passed and that still have attempts left.
- Bounce, complaint and delivery webhooks are normalised for SES (including SNS-wrapped payloads) and Postmark in `apps.hotel.email_webhooks`; the booking is matched by reference or provider message id.
- `/ops/` shows delivered, failed and bounced/complained email counts plus a table of problem deliveries with attempts, next retry and last error.
- The guest booking and voice pages pass an automated accessibility check (`tests/test_accessibility.py`) and an axe-core audit. The booking form has labelled controls and a labelled rate-plan radiogroup; the voice page announces call status through a live region and hides the avatar from assistive technology.
- The same check audits the design tokens in `static/css/app.css` for WCAG AA contrast: 4.5:1 for text (including semantic badges and alpha text over the dark hero, call card and footer) and 3:1 for focus rings and control borders.
- Throttling uses the database cache in production (created by the entrypoint) and an in-memory cache in tests.
- The Docker compose stack includes a `scheduler` sidecar that runs `expire_holds`, `reconcile_calendars --apply`, `send_booking_emails` and `send_booking_sms` every five minutes.
- Channel manager (v0.5.1): OTA iCal feeds are imported into per-room `RoomBlock` rows through `ChannelFeed`; availability, creation and modification honour them, and `Booking.clean()` rejects overlapping blocks. A signed `/calendar/<token>.ics` feed exports the hotel's blocking bookings (pending holds as `TENTATIVE`) and manual blocks. Conflicts with local blocking bookings are counted on the feed and surfaced on `/ops/`. The scheduler runs `sync_channel_feeds --apply`.

- After confirmation, the agent asks whether the guest wants the confirmation by email or SMS. It can use a known address/number or ask for one, then sends through `send_confirmation_email` or `send_confirmation_sms` (demo and LLM tool path).
- Demo/provider date parsing accepts natural inputs (e.g. “20th of November”, ranges without year, “from tomorrow for 5 days” / “od jutra na 3 noce”); tool date parsing is tolerant as a safety net.

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
| Magic-link guest account, not passwords | A guest already proves control of an email; a single-use hashed token avoids password storage, resets and a second credential to leak. |
| Login requests always get the same neutral response | The form must not reveal which email addresses have bookings; the email is only sent when a booking exists. |
| Guest sessions grant booking access | After a magic link, the session email is proof of identity, so `/booking/<ref>/` can issue its own token instead of forcing the guest back through the lookup form. |
| Fernet encryption for provider keys | Stops API keys appearing in plaintext in SQLite backups or the admin database dump; rotation is a management command. |
| Database cache for throttling | No Redis dependency for the pilot; the cache table survives restarts and is shared by gunicorn workers. |
| Email after commit, with an idempotency marker | A mail outage must not roll back a booking, and retries must not spam the guest. |
| Imported OTA reservations become `RoomBlock` rows, not bookings | Keeps the booking write path, availability and modification logic untouched; channel conflicts are counted, not auto-rejected. |
| Channel feed is per physical room | Matches the per-room availability invariant; one feed per room is the simplest correct mapping for an alpha. |
| Export feed uses a signed token (`django.core.signing`) | A room calendar URL is a secret subscription endpoint, not a public reference. |
| Haru full-duplex model not integrated | Requires a 12 GB GPU, no Polish support, no tool calling; incompatible with the alpha's CPU stack and booking agent. See `docs/research/haru-voice-model.md`. |
| Deterministic flexible date parsing | Demo/provider parsing and tolerant tool-date handling cover natural booking inputs (ordinals, ranges without year, relative start + duration) without adding an SLM dependency, preserving the offline/CPU-only fallback. |
| Polish is the default language | The pilot property is in Poland; a Polish greeting with an explicit English option matches how guests arrive and avoids forcing a language switch. The engine flips the stored conversation language when the guest asks. |
| SMS goes through a provider layer with a console default | Keeps the seven-step scenario and the tests working offline without credentials, while leaving a real Twilio adapter behind the same `SmsProvider` interface. |
| SMS delivery state and history live on the booking and `SmsDelivery` | Mirrors the email design: the confirmation marker makes retries idempotent, current status/error/provider-id/attempt fields feed the admin and API, and every attempt is recorded as a `SmsDelivery` row. Provider receipts refine the matching/latest row and current status; failures never roll back a reservation. |
| SMS webhooks are normalised and best-effort matched | Twilio/generic receipt and inbound payloads are parsed in `apps.hotel.sms_webhooks`; the API view authenticates with `SMS_WEBHOOK_SECRET` when set, matches receipts by reference or provider message id, stores inbound replies even when unmatched, and links a reply to a booking by normalized phone when a recent match exists. Unknown receipts are acknowledged. |
| SMS retries mirror email backoff | `confirmation_sms_next_retry_at` and the existing attempt fields give the scheduler the same idempotent retry queue as email: failed sends schedule exponential backoff, `send_booking_sms` only picks due unsent confirmations under the attempt cap, and force/manual resend remains explicit. |
| Handover is a recorded state change, not a channel-specific path | `transfer_to_human` stores the reason in `conversation.state` so chat and voice share one implementation and reception can see why the guest was escalated. |
| Prompt refresh migration only touches the old defaults | Existing databases pick up the seven-step prompts without overwriting an operator's customised text. |
| Cancellation policy in `apps.hotel.cancellation` | The page, API, agent tools and write path must quote the same fee; duplicating the rule in four places guarantees drift. |
| Policy is hotel-wide, not per rate plan | v0.3.0 has no rate plans yet; a single window and fee-night count matches the pilot's contract and stays explainable. |
| Fee = first N stored nightly rates, capped at the total | Uses the actual quote (including daily-rate overrides) instead of a fresh calculation, and can never exceed what the guest agreed to pay. |
| Pending holds are always free to release | The hold is not a commitment; charging for it would make the confirm step meaningless. |
| Modify in place through `modify_booking()` | Reuses `available_rooms(exclude_booking=...)` and `quote()` in one transaction, so availability stays per physical room and the audit trail stays on one reference. |
| `BookingEvent` is append-only in the model | `save()` rejects updates and the admin has no add/delete, so the trail cannot be rewritten even from the admin. |
| Ops dashboard is `/ops/`, not a custom admin index | `staff_member_required` gives the right access control without fighting the admin index; the page is read-only and reuses domain querysets. |
| `DailyRate` replaces the calculated rate and skips rules | One explicit row per room type/night is easier for reception to reason about than layering another rule type; the rate is the price. |
| Minimum stay is enforced in `quote()` | Keeps direct pricing, creation and modification honest; availability catches `StayError` and omits the room type so search never shows an unbookable option. |
| Rate calendar is an admin month grid | Reception already works in the admin; a grid over ordinary `DailyRate` rows avoids a parallel UI and keeps bulk import possible. |
| `/book/` quotes through `search_availability` | The web form must show the same prices, restrictions and rate-plan totals as chat and voice; re-implementing the pricing loop in the view would drift. |
| The web booking form confirms immediately | The guest has already entered their details and pressed the final button, so the hold has served its purpose; the confirmation email and calendar sync then follow the normal after-commit path. |
| Rate-plan radios are built from the availability payload | `search_availability` already filters plans by room type and stay restrictions; the view only enriches them with the admin description. |
| Retry/backoff lives on the booking, not in a queue table | The booking already carries the delivery marker and status; three extra fields give the scheduler everything it needs without a second store to keep in sync. |
| Backoff is exponential and capped by settings | A transient provider outage recovers quickly, while a permanently bad address stops after `EMAIL_MAX_ATTEMPTS` instead of hammering the provider forever. |
| Webhooks are normalised per provider before the view | SES (including SNS wrappers) and Postmark speak different payloads; keeping that translation in `email_webhooks` keeps the API view small and testable. |
| Provider message id is the fallback lookup key | Provider callbacks often omit our booking reference, so `confirmation_email_provider_id` lets a bounce still find the right reservation. |
| Accessibility is checked with a stdlib HTML audit in pytest | The repo has no Node build step, so the CI gate parses rendered pages for labels, landmarks, heading order and ARIA misuse; axe-core remains the manual cross-check. |
| Contrast is computed from the design tokens, not hardcoded pairs | Parsing `app.css` and compositing alpha colours over their real backdrop keeps the gate honest when tokens change; a short hardcoded list missed the semantic badges and the light-on-dark text. |
| Voice call status is a live region, not an aria-label on the button | The button's visible label changes from "Start call" to "End call"; a fixed `aria-label` would go stale and break Label in Name. |

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
.\.venv\Scripts\python.exe manage.py send_booking_sms
.\.venv\Scripts\python.exe manage.py sync_calendars --retry-failed
.\.venv\Scripts\python.exe manage.py sync_calendars --reference HA-XXXXXX
..\.venv\Scripts\python.exe manage.py reconcile_calendars
.\.venv\Scripts\python.exe manage.py reconcile_calendars --apply
.\.venv\Scripts\python.exe manage.py export_bookings --from 2026-01-01 --to 2026-12-31 -o bookings.csv
.\.venv\Scripts\python.exe manage.py rotate_provider_keys
```

Health: `/healthz` and `/api/v1/health/`. Admin: `/admin/`. API root: `/api/v1/`.

## Traps and invariants

- **One write path.** `apps.hotel.booking_service` owns create/confirm/modify/cancel/expire. Do not write bookings from views, tools or admin actions directly.
- **`/book/` creates and confirms through the service.** The view validates the form and quotes through `search_availability`, then calls `create_booking()` and `confirm_booking()`; it must not set `Booking.status` or prices itself.
- **The web booking form does not duplicate rate-plan arithmetic.** Plan totals come from the availability payload; the view only adds the admin description. Changing a plan's adjustment or applicability belongs in `pricing.quote()` / `RatePlan`.
- **Cancellation fees come from `cancellation_preview()`.** The page, API preview, agent tools and `cancel_booking()` must read that function; never re-implement the free window or fee arithmetic in a view or template.
- **Refresh a stale booking before previewing.** `confirm_booking()` and `cancel_booking()` return fresh objects; an in-memory instance from before the transition still says `pending` and previews the wrong reason/fee.
- **`BookingEvent` is append-only.** `save()` raises on an existing pk and the admin is read-only. Add a new event instead of editing history; do not add admin add/delete permissions.
- **Modification reuses `exclude_booking`.** Omitting it makes the booking block itself and every date change fails with `no_availability`.
- **`DailyRate.min_nights` can fail a quote.** `search_availability()` catches `StayError` and skips the room type; direct pricing, creation and modification surface `min_nights` instead. Do not swallow that error in the write path.
- **A `DailyRate` row skips pricing rules for that night.** Do not "apply rules on top" of an explicit rate; that changes the meaning of the calendar.
- **Availability is per physical room**, not per room type. Two holds for the same room with overlapping nights must be rejected.
- **Pending holds block only until `hold_expires_at`.** Null or future deadlines block; past deadlines do not. `expire_holds` runs in the compose `scheduler` sidecar (or host cron).
- **Calendar sync and confirmation email must stay after commit.** Calling either inside the transaction reintroduces rollback coupling and can email a guest about a booking that then rolls back.
- **Confirmation retries respect the stored backoff.** `send_pending_confirmation_emails()` skips bookings whose `next_retry_at` is in the future or that have reached `EMAIL_MAX_ATTEMPTS`. `--force` is the deliberate override; do not remove the filters from the scheduler path.
- **Every send attempt updates the retry fields.** `send_booking_confirmation()` must increment `confirmation_email_attempts`, stamp `confirmation_email_last_attempt_at`, and set or clear `confirmation_email_next_retry_at`; otherwise the dashboard and scheduler drift from reality.
- **Webhook payloads are normalised in `email_webhooks.py`.** Keep SES/SNS and Postmark field names there, not in the API view. The view only resolves the booking and writes the status.
- **SMS delivery follows the same after-commit and idempotency rules as email.** `send_booking_confirmation_sms()` owns `confirmation_sms_sent_at`, the current status/attempt/error/provider-id/retry fields, and appends a `SmsDelivery` row for each attempt; a send failure must never roll back the booking. `force=True` is the deliberate resend path (admin action or command override), not the default. Automatic retries are driven by `send_booking_sms`, which honours `confirmation_sms_next_retry_at` and `SMS_MAX_ATTEMPTS` like email.
- **SMS webhooks match receipts best-effort.** `/api/v1/sms/webhooks/<provider>/` requires `X-Sms-Webhook-Secret` when `SMS_WEBHOOK_SECRET` is set, resolves receipts by booking reference or provider message id (booking field or `SmsDelivery` row, falling back to the latest delivery), updates booking and matching/latest `SmsDelivery`, and returns ignored for unknown receipts. Inbound replies are always stored as `InboundSmsMessage` and linked to a booking only when a normalized phone match is found among recent bookings.
- **`ConsoleSmsProvider.outbox` is class-level.** Tests reset it through the autouse fixture in `tests/conftest.py`; do not assert across tests without a reset.
- **The default language is Polish.** Web tests that assert English copy must pass `HTTP_ACCEPT_LANGUAGE="en"` or wrap the call in `translation.override("en")`; otherwise they see the Polish catalogue.
- **Language switching is detected before each turn.** `apps/agents/engine.py` flips `conversation.language` on an explicit request for English/Polish; the demo provider and prompts then render in that language. Do not hard-code a language in a channel path.
- **Guest pages must pass `tests/test_accessibility.py`.** It is a structural gate: one `<h1>`, a `<main>` landmark, a skip link, labelled controls, named buttons and links, no positive tabindex and no focusable content under `aria-hidden`. Add new guest-facing pages to the test rather than weakening a rule.
- **Do not put a fixed `aria-label` on the voice call button.** Its visible label switches between "Start call" and "End call"; a static accessible name breaks WCAG Label in Name. Name the button from its visible text and announce status changes through the `role="status"` region.
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
- **Shell scripts need the git exec bit.** `docker/entrypoint.sh` and `docker/scheduler.sh` must stay mode `100755`; a Windows checkout plus a local Docker build hides a `100644` mode, but the Linux CI smoke job fails with `permission denied`. The Dockerfile also runs `chmod +x` as a belt-and-braces fix.
- **Colour tokens have contrast floors.** `--gold` is decorative; use `--gold-ink` for text on light backgrounds, `--focus-ring` for focus outlines and `--line-strong` for control borders. `tests/test_accessibility.py` fails if a pair drops below 4.5:1 (text) or 3:1 (focus rings and control borders).
- **Demo provider guest count must survive turns.** `_asked_guests` only looked at the last assistant message, which after a tool call is empty, so a bare-number answer ("2") was forgotten and the agent re-asked guests forever. Recover the count by scanning the full message list for the guest question and the answer that followed it.
- **Bare "1"/"2" are language choices only at the start.** `detect_language` used to treat any bare "2" as "English", which flipped a Polish conversation to English when the guest answered the guest-count question with "2". Number-based switching is now disabled once the conversation has user messages.

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
| 2026-11-13 | v0.3.0 | `pytest` | 123 passed |
| 2026-11-13 | v0.3.0 | `manage.py check` | 0 issues |
| 2026-11-13 | v0.3.0 | `makemigrations --check` | no changes detected |
| 2026-11-13 | v0.3.0 | `ruff check .` | clean |
| 2026-11-13 | v0.3.0 | Polish catalogue | 468 strings extracted, 0 missing |
| 2026-11-13 | v0.3.0 | Docker `docker compose up --build -d` | image built; `web` healthy, `scheduler` started, `/healthz` returns `{"status": "ok"}` |
| 2026-11-13 | v0.3.0 | Docker scheduler sidecar | logs `Hold cleanup scheduler started (every 300s)` then `Expired 0 booking hold(s).` |
| 2026-11-13 | v0.3.0 | Docker API booking flow | create → 403 without token → 200 with token; confirm; modify dates (price recalculated); cancel with reason recorded; audit events created/confirmed/modified/cancelled |
| 2026-11-13 | v0.3.0 | Docker rate calendar | month grid rendered; saving a row wrote `DailyRate` (rate + min_nights); one-night search omitted the room type; two-night search showed the override rate |
| 2026-11-13 | v0.3.0 | Docker ops dashboard | `/ops/` rendered arrivals/departures/holds/failed syncs/email gaps/recent events; staff login required |
| 2026-11-13 | v0.3.0 | Docker pages EN/PL | home, booking detail and `/ops/` rendered in both languages; cancellation policy and modify form translated |
| 2026-11-13 | v0.3.0 | GitHub Actions CI | green on `main` and `develop` (tests + Docker build) |
| 2026-11-14 | v0.4.11 | `pytest` | 152 passed |
| 2026-11-14 | v0.4.11 | `manage.py check` | 0 issues |
| 2026-11-14 | v0.4.11 | `makemigrations --check` | no changes detected |
| 2026-11-14 | v0.4.11 | `ruff check .` | clean |
| 2026-11-14 | v0.4.11 | Polish catalogue | 555 strings extracted, 0 missing |
| 2026-11-14 | v0.4.11 | `/book/` locally EN/PL | 200; quote and guest form rendered |
| 2026-11-14 | v0.4.11 | Docker compose build + `/healthz` | image built; `/healthz` returns `{"status": "ok"}` |
| 2026-11-14 | v0.4.11 | Docker web booking EN/PL | two bookings confirmed (`HA-QLW54V`, `HA-V45S3Q`), source `web`, confirmation email `sent` |
| 2026-11-14 | v0.4.11 | Docker rate-plan booking | plan shown in `/book/`; `HA-LCRYX2` stored the plan, total `756.00`, email `sent` |
| 2026-11-14 | v0.4.12 | `pytest` | 162 passed |
| 2026-11-14 | v0.4.12 | `manage.py check` | 0 issues |
| 2026-11-14 | v0.4.12 | `makemigrations --check` | no changes detected |
| 2026-11-14 | v0.4.12 | `ruff check .` | clean |
| 2026-11-14 | v0.4.12 | Polish catalogue | 569 strings extracted, 0 missing |
| 2026-11-14 | v0.5.0 | `pytest` | 167 passed |
| 2026-11-14 | v0.5.0 | `manage.py check` | 0 issues |
| 2026-11-14 | v0.5.0 | `makemigrations --check` | no changes detected |
| 2026-11-14 | v0.5.0 | `ruff check .` | clean |
| 2026-11-14 | v0.5.0 | Polish catalogue | 568 strings extracted, 0 missing |
| 2026-11-14 | v0.5.0 | axe-core (`/book/`, quote, `/voice/`, EN/PL) | 0 violations, 0 incomplete |
| 2026-11-14 | v0.5.0 | `tests/test_accessibility.py` | 5 passed; negative fixture flagged duplicate ids, missing lang/title, unlabelled controls and hidden focusables |
| 2026-11-14 | v0.5.0 | Docker web booking | `HA-P6WR73` created and confirmed from `/book/`, source `web`, email `sent` |
| 2026-11-14 | v0.5.0 | Docker SES/Postmark webhooks | bounce and delivery matched; `/ops/` showed the booking |
| 2026-11-14 | v0.5.0 | Docker compose build + `/healthz` | image built; `/healthz` returned `{"status": "ok"}` |
| 2026-11-14 | v0.5.1 | `pytest` | 180 passed |
| 2026-11-14 | v0.5.1 | `manage.py check` | 0 issues |
| 2026-11-14 | v0.5.1 | `makemigrations --check` | no changes detected |
| 2026-11-14 | v0.5.1 | `ruff check .` | clean |
| 2026-11-14 | v0.5.1 | Polish catalogue | 595 strings extracted, 0 missing |
| 2026-11-14 | v0.5.1 | `tests/test_channel_manager.py` | 13 passed |
| 2026-11-14 | v0.5.1 | Docker compose build + `/healthz` | image built; `/healthz` returned `{"status": "ok"}` |
| 2026-11-14 | v0.6.8 | `pytest` | 188 passed |
| 2026-11-14 | v0.6.8 | `manage.py check` | 0 issues |
| 2026-11-14 | v0.6.8 | `makemigrations --check` | no changes detected |
| 2026-11-14 | v0.6.8 | `ruff check .` | clean |
| 2026-11-15 | v0.6.9 | `pytest -o addopts=""` | 189 passed |
| 2026-11-15 | v0.6.9 | `manage.py check` | 0 issues |
| 2026-11-15 | v0.6.9 | `makemigrations --check --dry-run` | no changes detected |
| 2026-11-15 | v0.6.9 | `ruff check .` | clean |
| 2026-11-15 | v0.6.9 | Polish catalogue | 617 strings extracted, 617 translated, compiled |
| 2026-11-15 | v0.6.9 | Demo flow (chat + voice) | availability → hold → confirm → email/SMS exercised in EN and PL |
| 2026-11-15 | v0.6.9 | Pages EN/PL (`/`, `/chat/`, `/voice/`, `/book/`, `/my-bookings/`, `/booking/`) | all 200 with matching `lang` |
| 2026-11-15 | v0.6.9 | Docker compose build + `/healthz` | image built; `/healthz` returned `{"status": "ok"}` |
| 2026-11-15 | v0.6.9 | Docker pages EN/PL | `/`, `/chat/`, `/voice/`, `/book/` all 200 with matching `lang` |
| 2026-11-15 | v0.6.9 | GitHub Actions CI (`develop`) | green: test, docker and smoke jobs pass after the entrypoint permission fix |
| 2026-11-19 | v0.6.10 | `pytest` | 193 passed (3 new guest-count/language regressions) |
| 2026-11-19 | v0.6.10 | `manage.py check` | 0 issues |
| 2026-11-19 | v0.6.10 | `makemigrations --check --dry-run` | no changes detected |
| 2026-11-19 | v0.6.10 | `ruff check .` | clean |
| 2026-11-19 | v0.6.10 | Demo flow EN + PL | bare "2" guest answer asks the count once and completes the booking; PL stays Polish |
| 2026-11-19 | v0.6.10 | Docker compose build + `/healthz` | image built; `/healthz` returned `{"status": "ok"}` |
| 2026-11-19 | v0.6.10 | Docker pages EN/PL (`/`, `/chat/`, `/voice/`, `/book/`, `/my-bookings/`) | all 200 with matching `lang` |
| 2026-11-19 | v0.6.10 | GitHub Actions CI (`main` + `develop`) | green: test, docker and smoke jobs pass |
| 2026-11-19 | v0.7.0 (unreleased) | `pytest` | 201 passed (8 new email-history tests) |
| 2026-11-19 | v0.7.0 (unreleased) | `manage.py check` | 0 issues |
| 2026-11-19 | v0.7.0 (unreleased) | `makemigrations --check --dry-run` | no changes detected |
| 2026-11-19 | v0.7.0 (unreleased) | `ruff check .` | clean |
| 2026-11-19 | v0.7.0 (unreleased) | Polish catalogue | 622 strings extracted, all translated and compiled |
| 2026-11-19 | v0.7.0 (unreleased) | `/ops/` EN + PL | email-history section renders in both languages; staff-only gate unchanged |
| 2026-11-19 | v0.7.0 (unreleased) | Scheduler load (`tests/test_scheduler_load.py`) | 200 stale holds and 100 pending confirmation emails processed within budget (2 tests passed) |
| 2026-11-19 | v0.7.0 (unreleased) | `pytest` | 212 passed (includes SMS send/receipt/inbound/auth/ops/admin tests) |
| 2026-11-19 | v0.7.0 (unreleased) | `manage.py check` | 0 issues |
| 2026-11-19 | v0.7.0 (unreleased) | `makemigrations --check --dry-run` | no changes detected (0015 applied to models) |
| 2026-11-19 | v0.7.0 (unreleased) | `ruff check .` | clean |
| 2026-11-19 | v0.7.0 (unreleased) | Polish catalogue | 642 strings extracted, 643 translated and compiled |
| 2026-11-19 | v0.7.0 (unreleased) | GitHub Actions CI (PR #3, `feature/v0.7.0-sms-receipts-replies`) | green: test, docker and smoke jobs pass |

Update this table when the release gate is re-run.

## Open questions for v0.7.0

- ~~Colour contrast: add automated checks to the accessibility gate, or keep as manual review?~~ Resolved: the token audit now enforces WCAG AA for text, focus rings and control borders.
- ~~Email history: per-message view vs. only current delivery state.~~ Resolved: each send/retry appends one `EmailDelivery` row; `/ops/` shows the last 20 rows and the admin has a read-only list with `subject`. Provider webhooks update the matching row's final status in place — one row per attempt, not per status transition.
- ~~Scheduler: load-test with a large backlog.~~ Resolved: `tests/test_scheduler_load.py` covers 200 expired holds and 100 pending confirmation emails; both complete well within the 10 s budget.
- ~~SMS: add inbound replies and delivery-receipt webhooks, or keep outbound-only for the pilot?~~ Resolved: each send appends a `SmsDelivery` row, provider receipts update booking/current delivery status via `/api/v1/sms/webhooks/<provider>/`, and inbound replies are stored as `InboundSmsMessage` records (linked by phone when possible) with read-only surfacing in `/ops/` and the admin.

## Next move

v0.7.0 is released and tagged (`v0.7.0`, `b4e9b60`) on `main`. **v0.8.0 (unreleased, on `develop`)** includes SMS retry/backoff parity (PR #4, `5418604`), scheduler load coverage for pending SMS confirmations (PR #5, `d39606d`), and scheduler load coverage for due SMS retry backlogs (`tests/test_scheduler_load.py`). No release has been cut. Next: choose the next slice (or prepare a v0.8.0 release if asked) and plan it in [docs/ROADMAP.md](docs/ROADMAP.md) before implementation.
