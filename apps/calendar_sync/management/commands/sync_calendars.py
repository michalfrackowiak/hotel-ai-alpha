"""Push bookings to their room calendars."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.calendar_sync.models import BookingCalendarEvent
from apps.calendar_sync.services import retry_pending_events, sync_all_bookings, sync_booking
from apps.hotel.booking_service import BookingError, get_booking


class Command(BaseCommand):
    help = "Synchronise confirmed bookings with their room calendars."

    def add_arguments(self, parser):
        parser.add_argument("--reference", help="Sync a single booking by reference.")
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Retry calendar events that previously failed.",
        )

    def handle(self, *args, **options):
        if options["retry_failed"]:
            count = retry_pending_events()
            self.stdout.write(f"Retried {count} failed calendar event(s).")

        reference = options.get("reference")
        if reference:
            try:
                booking = get_booking(reference)
            except BookingError as exc:
                raise CommandError(exc.message) from exc
            record = sync_booking(booking)
            if record is None:
                self.stdout.write(f"{reference}: no active calendar mapping (skipped).")
            elif record.status == BookingCalendarEvent.Status.FAILED:
                raise CommandError(f"{reference}: {record.error_message}")
            else:
                self.stdout.write(self.style.SUCCESS(f"{reference}: {record.get_status_display()}"))
            return

        summary = sync_all_bookings()
        self.stdout.write(
            self.style.SUCCESS(
                f"Calendar sync complete: {summary['synced']} synced, "
                f"{summary['failed']} failed, {summary['skipped']} skipped."
            )
        )
