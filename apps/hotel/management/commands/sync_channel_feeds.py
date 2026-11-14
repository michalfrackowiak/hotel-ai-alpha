"""Import channel-manager iCal feeds into room blocks."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.hotel.channel_feeds import sync_all_feeds


class Command(BaseCommand):
    help = "Synchronise OTA iCal feeds into room blocks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command is a read-only dry run.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        summary = sync_all_feeds(dry_run=not apply)

        for result in summary["results"]:
            if not result.ok:
                self.stdout.write(f"{result.feed.name}: failed — {result.error}")
                continue
            self.stdout.write(
                f"{result.feed.name}: {result.event_count} event(s), "
                f"+{result.created} ~{result.updated} -{result.deleted}, "
                f"{result.conflicts} conflict(s)"
            )

        mode = "applied" if apply else "dry run"
        self.stdout.write(
            self.style.SUCCESS(
                f"Channel sync ({mode}): {summary['feeds']} feed(s), "
                f"{summary['ok']} ok, {summary['failed']} failed, "
                f"{summary['created']} created, {summary['updated']} updated, "
                f"{summary['deleted']} deleted, {summary['conflicts']} conflict(s)."
            )
        )
