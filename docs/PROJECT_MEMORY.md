# Project memory

Living notes for whoever picks this up next. Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md), endpoints in [API.md](API.md), plans in [ROADMAP.md](ROADMAP.md) and the branch/release process in [BRANCHING.md](BRANCHING.md). This file records state, decisions and traps that those documents do not.

**Last updated:** 13 November 2026 · **Version:** v0.1.0 · **Tests:** 62 passing

## Current state

- Phases 0–7 of the alpha are complete. The app books a room end-to-end by chat and voice in English and Polish.
- Local database `data/db.sqlite3` is migrated and seeded. The local superuser is `admin` / `AuroraAdmin!2026` (demo only, `.env` is gitignored).
- Git is initialised with `main` and `develop`; `v0.1.0` is tagged and pushed to `github.com/michalfrackowiak/hotel-ai-alpha`.
- The default AI provider is the offline `demo` policy. No external API key is required to run or demo the app.
- The demo calendar connection uses the `fake` provider, so confirmation and cancellation exercise the real sync path without Google credentials.

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
| SQLite + one gunicorn container | Deliberate alpha scope; concurrency and multi-worker throttling are v0.2 concerns. |

## Runbook

All commands assume the repository root and the local virtualenv.

```powershell
# Setup
.\.venv\Scripts\python.exe manage.py migrate
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
.\.venv\Scripts\python.exe manage.py sync_calendars --retry-failed
.\.venv\Scripts\python.exe manage.py sync_calendars --reference HA-XXXXXX
```

Health: `/healthz` and `/api/v1/health/`. Admin: `/admin/`. API root: `/api/v1/`.

## Traps and invariants

- **One write path.** `apps.hotel.booking_service` owns create/confirm/cancel/expire. Do not write bookings from views, tools or admin actions directly.
- **Availability is per physical room**, not per room type. Two holds for the same room with overlapping nights must be rejected.
- **Pending holds block only until `hold_expires_at`.** Null or future deadlines block; past deadlines do not. `expire_holds` is not yet scheduled automatically.
- **Calendar sync must stay after commit.** Calling it inside the transaction reintroduces rollback coupling.
- **Pending bookings are skipped by `sync_booking` before any event record is created.** This is intentional and tested.
- **The demo provider has no `language` tool argument.** Language is carried by the system prompt and conversation; adding vendor-specific arguments breaks the demo policy.
- **`AgentSettings` is a singleton at `pk=1`.** `load()` creates it; `save()` forces the pk.
- **`AIProvider.save()` enforces a single default.** Marking one default clears the others; the registry reads the first active default.
- **Admin model forms use explicit `fields`.** Ruff's `DJ007` rule rejects `fields = "__all__"`; keep the field lists in sync with the models.
- **Polish strings are extracted from templates and Python**, then filled from `scripts/pl_translations.py`. Edit the dictionary and re-run both scripts; do not hand-edit only the `.mo`.
- **Translation scripts import `pl_translations` as a sibling module.** Run them by path from the repo root, as shown above.
- **The test settings must keep throttling off and use the fake calendar.** Otherwise API tests become flaky or make network calls.
- **The browser speech stack is vendor-dependent.** Chrome/Edge are the supported demo browsers; the voice page falls back to chat.
- **`.env` is not committed.** Keep provider keys, superuser credentials and Google credentials out of git.
- **Ruff ignores `E501` but enforces import order, unused imports and Django rules.** Run it before pushing; CI does not currently run ruff, so this is on the developer.

## Verification log

| Date | Check | Result |
| --- | --- | --- |
| 2026-11-13 | `pytest` | 62 passed |
| 2026-11-13 | `manage.py check` | 0 issues |
| 2026-11-13 | `ruff check .` | clean after import/field fixes |
| 2026-11-13 | Docker compose build + `/healthz` | image built; `/healthz` returns `{"status": "ok"}` |
| 2026-11-13 | Browser booking EN/PL | chat flow confirmed two bookings; fake calendar events synced |
| 2026-11-13 | GitHub Actions CI | green on `main` and `develop` |

Update this table when the release gate is re-run.

## Open questions for v0.2.0

- Email provider: SMTP settings in `.env` vs. a transactional API (Resend/Postmark)?
- Hold cleanup: cron in the host, a compose sidecar, or a management command triggered by the platform scheduler?
- Throttling backend: database cache is enough for one container; Redis only if the deployment grows.
- Booking access: signed link, one-time code, or both?
- Calendar reconciliation: compare by event id only, or also compare start/end and summary?
- Key encryption: Django `SECRET_KEY`-derived Fernet, or an external secret manager?

## Next move

Start v0.2.0 on `develop` from the `v0.1.0` tag. Recommended order:

1. Email confirmations (guest-facing value, forces a notification abstraction).
2. Booking access control (security, changes lookup URLs and tests).
3. Scheduled hold cleanup + persistent throttling (operational safety).
4. Provider key encryption and calendar reconciliation (hardening).
5. Update roadmap, this file and `docs/releases/v0.2.0.md` before tagging.
