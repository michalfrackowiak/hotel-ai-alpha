#!/bin/sh
# Small sidecar that releases expired booking holds on a fixed interval.
# The web container owns migrations and cache-table creation; this process
# only runs management commands, so it can start after the web health check.
set -eu

interval="${HOLD_CLEANUP_INTERVAL_SECONDS:-300}"

echo "==> Hold cleanup scheduler started (every ${interval}s)"
while true; do
    python manage.py expire_holds || echo "expire_holds failed; will retry"
    python manage.py reconcile_calendars --apply || echo "reconcile_calendars failed; will retry"
    python manage.py send_booking_emails || echo "send_booking_emails failed; will retry"
    python manage.py send_booking_sms || echo "send_booking_sms failed; will retry"
    python manage.py sync_channel_feeds --apply || echo "sync_channel_feeds failed; will retry"
    python manage.py openline_prune --days "${OPENLINE_WEBHOOK_RETENTION_DAYS:-30}" || echo "openline_prune failed; will retry"
    python manage.py prune_phone_data --apply --days "${VOICE_PHONE_RETENTION_DAYS:-30}" || echo "prune_phone_data failed; will retry"
    sleep "$interval"
done
