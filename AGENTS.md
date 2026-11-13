# AGENTS.md

Working notes for coding agents in this repository. Read this first, then reach for the document that matches the task.

## What this is

Hotel Aurora is a Django 5.2 + DRF alpha of an AI hotel receptionist. Guests book by chat or voice in English and Polish. Availability, pricing and bookings come from Django models; an LLM only chooses which tools to call. SQLite, Docker, no Node build step.

Current release: **v0.1.0**, 62 tests passing. The next milestone is planned in [docs/ROADMAP.md](docs/ROADMAP.md).

## Commands

Use the local virtualenv; do not rely on a global Python.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe manage.py runserver
docker compose up --build
```

The gate for any change is: `pytest` green, `manage.py check` clean, `ruff check .` clean. CI also builds the Docker image.

## Where to look

| Task | Read |
| --- | --- |
| Understand the system, domain model, agent loop, calendar design | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Add or change an API endpoint | [docs/API.md](docs/API.md), `apps/api/` |
| Plan or check milestone status | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Branch, commit or release | [docs/BRANCHING.md](docs/BRANCHING.md) |
| Recall decisions, traps and the runbook | [docs/PROJECT_MEMORY.md](docs/PROJECT_MEMORY.md) |
| Set up, configure providers, voice or Google Calendar | [README.md](README.md) |

## Invariants

- **Bookings have one write path:** `apps/hotel/booking_service.py`. Views, tools and admin call it; they never write booking state themselves.
- **Availability is per physical room.** Overlapping blocking bookings for the same room must be rejected; pending holds block only until `hold_expires_at`.
- **Calendar sync runs after commit.** Keep `transaction.on_commit`; a calendar failure must never roll back a reservation. Pending bookings are skipped before an event record is created.
- **The agent affects the world only through tools.** Tool handlers return `{"ok": false, "error": ...}` on bad input or domain errors instead of raising.
- **The demo provider is the offline fallback.** It must keep working without API keys and must stay in sync with the tool schemas. It takes no `language` tool argument.
- **Chat and voice share one conversation engine.** Add behaviour to the engine/tools, not to a channel-specific path.
- **Polish translations are generated.** Edit `scripts/pl_translations.py`, then run `scripts/update_translations.py` and `scripts/compile_translations.py`. Commit the `.po` and `.mo`.
- **Admin model forms use explicit `fields`.** `ruff` rule `DJ007` rejects `fields = "__all__"`.
- **Secrets stay out of git.** `.env`, `data/`, `staticfiles/`, provider keys and Google credentials are ignored; keep them that way.

## Conventions

- Keep the existing app boundaries: `hotel` (domain), `api` (HTTP), `agents` (engine/tools/prompts), `providers` (LLM adapters), `voice` (speech), `calendar_sync` (calendar), `web` (pages).
- Match the surrounding style: `from __future__ import annotations`, type hints on service functions, `gettext_lazy` for user-facing Python strings, `{% trans %}` in templates.
- Line length 110, Python 3.12 target. Ruff enforces import order, unused imports, bugbear and Django rules.
- New user-facing text needs both an English source string and an entry in the Polish dictionary.
- Tests live in `tests/` and use pytest + pytest-django. Prefer the existing fixtures (`rooms`, `booking`, `api_client`) over new setup.
- Database changes need a committed migration. Never edit an applied migration on a shared branch.
- Update `docs/ROADMAP.md`, `docs/PROJECT_MEMORY.md` and `docs/releases/` when a milestone or release changes.

## Verify before you finish

1. Run the three gate commands above.
2. If the change touches a page, load it locally in English and Polish.
3. If it touches Docker, settings or static files, run `docker compose up --build` and check `/healthz`.
4. If it touches the agent, exercise a full availability → hold → confirmation flow with the demo provider.
5. Report exactly which checks ran and their result; do not claim a check you did not run.
