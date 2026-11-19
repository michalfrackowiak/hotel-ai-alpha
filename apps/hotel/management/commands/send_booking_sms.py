"""Retry confirmation SMS messages that have not been delivered yet."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.hotel.notifications import send_pending_confirmation_sms


class Command(BaseCommand):
    help = (
        "Retry confirmation SMS messages that are due, honouring the exponential backoff "
        "stored on each booking."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even when the confirmation SMS was already marked as sent.",
        )

    def handle(self, *args, **options):
        sent = send_pending_confirmation_sms(force=options["force"])
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} confirmation SMS message(s)."))
