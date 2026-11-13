# Branching and releases

This repository uses a lightweight Git Flow. `main` always reflects a released alpha; `develop` is the integration branch for the next milestone.

## Branches

| Branch | Purpose | Deploys |
| --- | --- | --- |
| `main` | Released, tagged code only | Demo/staging |
| `develop` | Integration branch for the next version | Never automatically |
| `feature/<version>-<topic>` | New behaviour, branched from `develop` | No |
| `fix/<version>-<topic>` | Bug fixes, branched from `develop` (or `main` for a hotfix) | No |
| `release/<version>` | Release hardening and notes, branched from `develop` | Staging |
| `hotfix/<version>` | Urgent fix branched from `main`, merged back to `main` and `develop` | After merge |

Examples:

```text
feature/v0.2.0-email-confirmations
fix/v0.2.0-hold-expiry-timezone
release/v0.2.0
hotfix/v0.1.1-calendar-retry
```

## Day-to-day flow

1. Branch from an up-to-date `develop`.
2. Keep commits small and focused. Use an imperative subject line, for example `Add email confirmation on booking`, `Fix hold expiry in Warsaw time`.
3. Run the local gate before pushing:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest
   .\.venv\Scripts\python.exe manage.py check
   .\.venv\Scripts\python.exe -m ruff check .
   ```

4. Open a pull request into `develop`. CI must be green: Django checks, the full pytest suite and the Docker image build.
5. Squash or rebase to keep history readable. Do not merge with failing checks.
6. Delete the branch after merge.

## Release flow

1. Cut `release/<version>` from `develop`.
2. Update:
   - `pyproject.toml` `version`;
   - `docs/ROADMAP.md` status and next-milestone plan;
   - `docs/PROJECT_MEMORY.md`;
   - `docs/releases/<version>.md`;
   - `README.md` when setup or behaviour changed.
3. Verify the release gate:
   - `pytest` green;
   - `manage.py check` clean;
   - `ruff check` clean;
   - `docker compose up --build` serves `/healthz`;
   - a browser booking works in English and Polish.
4. Merge `release/<version>` into `main` (fast-forward or merge commit, consistently).
5. Tag `v<version>` and push the tag:

   ```powershell
   git checkout main
   git pull --ff-only
   git tag -a v0.2.0 -m "Hotel Aurora v0.2.0"
   git push origin main --tags
   ```

6. Merge `main` back into `develop` so the version bump and notes are not lost.

## Hotfixes

1. Branch `hotfix/<version>` from `main`.
2. Fix, test, and bump the patch version and release notes.
3. Merge into `main`, tag `v<version>`, then merge `main` into `develop`.

## Commit conventions

There is no enforced commit-lint in the alpha, but these prefixes keep history searchable:

- `Add` — new behaviour or file.
- `Update` — change to existing behaviour, docs or dependencies.
- `Fix` — bug fix.
- `Refactor` — no behaviour change.
- `Test` — tests only.
- `Docs` — documentation only.
- `Chore` — tooling, CI, dependencies.

Reference the milestone in the body when it helps: `Part of v0.2.0 operational confidence.`

## Tags and versioning

- Semantic versioning: `vMAJOR.MINOR.PATCH`.
- `v0.x` is alpha: minor versions may change APIs and the database schema.
- Every release tag has a matching `docs/releases/v<version>.md`.
- Migrations are committed with the code that needs them; never rewrite an applied migration on a shared branch.

## What must not be committed

`.env`, `data/`, `staticfiles/`, `__pycache__/`, `.venv/`, local database copies, provider keys, Google credentials and OAuth tokens. They are covered by `.gitignore` and `.dockerignore`; check `git status` before the first push.
