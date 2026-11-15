#!/bin/sh
set -eu

echo "==> Applying database migrations"
python manage.py migrate --noinput

echo "==> Ensuring the persistent cache table exists"
python manage.py createcachetable

echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear >/dev/null

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
    echo "==> Seeding demo data (idempotent)"
    python manage.py seed_demo
fi

echo "==> Ensuring superuser exists"
python manage.py bootstrap_superuser

echo "==> Starting: $*"
exec "$@"
