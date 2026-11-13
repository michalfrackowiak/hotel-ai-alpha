"""Release expired pending booking holds."""

from django.core.management.base import BaseCommand

from apps.hotel.booking_service import expire_stale_holds


class Command(BaseCommand):
    help = "Mark pending bookings whose hold expired as expired."

    def handle(self, *args, **options):
        count = expire_stale_holds()
        self.stdout.write(f"Expired {count} booking hold(s).")
