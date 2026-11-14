"""Retry confirmation emails that have not been delivered yet."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.hotel.notifications import send_pending_confirmation_emails


class Command(BaseCommand):
    help = (
        "Retry confirmation emails that are due, honouring the exponential backoff "
        "stored on each booking."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even when the confirmation was already marked as delivered.",
        )

    def handle(self, *args, **options):
        sent = send_pending_confirmation_emails(force=options["force"])
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} confirmation email(s)."))
