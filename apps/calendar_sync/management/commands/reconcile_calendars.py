"""Detect and repair drift between bookings and their room calendars."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.calendar_sync.services import reconcile_calendars


class Command(BaseCommand):
    help = "Compare bookings with their calendar events and optionally repair differences."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Repair the issues found. Without this flag the command is a read-only dry run.",
        )
        parser.add_argument("--reference", help="Only reconcile a single booking by reference.")

    def handle(self, *args, **options):
        result = reconcile_calendars(apply=options["apply"], reference=options.get("reference"))
        summary = result["summary"]

        for issue in result["issues"]:
            state = "repaired" if issue["repaired"] else "dry-run" if not result["apply"] else "failed"
            line = (
                f"{issue['reference']}: {issue['issue']} ({issue['action']}) "
                f"event={issue['event_id'] or '—'} [{state}]"
            )
            if issue["error"]:
                line = f"{line} — {issue['error']}"
            self.stdout.write(line)

        mode = "applied" if result["apply"] else "dry run"
        self.stdout.write(
            self.style.SUCCESS(
                f"Reconciliation ({mode}): checked {summary['checked']}, ok {summary['ok']}, "
                f"missing {summary['missing']}, modified {summary['modified']}, "
                f"not synced {summary['not_synced']}, failed {summary['failed']}, "
                f"stale {summary['stale']}, repaired {summary['repaired']}."
            )
        )
