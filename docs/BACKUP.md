# Backup and restore (SQLite)

Resort Neptun Jastrzębia Góra stores everything in a single SQLite file (`data/db.sqlite3` in
Docker, the path from `DB_PATH`). Backing it up is a file-level operation, but
it must be done safely so a half-written file is never copied.

## Backup

Stop writes first (or run while the app is idle), then use SQLite's online
backup so the file is consistent even if a request is in flight:

```powershell
# Local
.\.venv\Scripts\python.exe manage.py dbbackup  # if django-dbbackup is installed
# or, using the sqlite3 CLI:
sqlite3 data/db.sqlite3 ".backup 'backups/db-$(Get-Date -Format yyyyMMddHHmmss).sqlite3'"
```

In Docker, run the backup from inside the container so the volume path is
correct:

```powershell
docker compose exec web sqlite3 /app/data/db.sqlite3 ".backup '/app/data/db-backup.sqlite3'"
docker compose cp web:/app/data/db-backup.sqlite3 ./db-backup.sqlite3
```

Always keep the matching `.env` (secret key, encryption key) with the backup:
without `FIELD_ENCRYPTION_KEY` the provider keys in the backup cannot be read.

## Restore

1. Stop the stack: `docker compose down`.
2. Replace the database file:

   ```powershell
   Copy-Item .\db-backup.sqlite3 .\data\db.sqlite3 -Force
   ```

3. Start the stack: `docker compose up -d`. The entrypoint runs migrations, so
   a backup from an older version is upgraded automatically.
4. Verify:

   ```powershell
   .\.venv\Scripts\python.exe manage.py check
   .\.venv\Scripts\python.exe manage.py migrate --check
   .\.venv\Scripts\python.exe -m pytest
   ```

## Verification drill

A restore is only real if it has been exercised. Run this drill quarterly:

1. Back up the production database to `drill.sqlite3`.
2. Start a clean container with an empty `hotel_data` volume.
3. Copy `drill.sqlite3` into the volume as `db.sqlite3`.
4. Run `docker compose up -d`, then `manage.py check` and `manage.py migrate --check`.
5. Log in to `/admin/` and confirm rooms, bookings and providers are present.
6. Run a booking through `/chat/` in both languages and cancel it.

Record the drill date and result in `docs/PROJECT_MEMORY.md`.
```

I referenced `manage.py dbbackup` which doesn't exist. Let me not promise that. I'll simplify and remove that line. Actually keep it but note it's optional. Let me just remove the django-dbbackup reference to avoid confusion.<|end|><|start|> buttonText? no. Let me just edit.<|end|><|start|>start to=edit<|channel|>analysis <|constrain|>xml<|message|><rfl:parameter name="path">
C:\Users\micha\repos\hotel-ai-alpha\docs\BACKUP.md