"""Export bookings, cancellations and fees to CSV for a date range."""

from __future__ import annotations

import csv
import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.hotel.models import Booking


class Command(BaseCommand):
    help = "Export bookings (including cancellations and fees) to CSV for a date range."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", default=None, help="Start date (YYYY-MM-DD), inclusive.")
        parser.add_argument("--to", dest="date_to", default=None, help="End date (YYYY-MM-DD), inclusive.")
        parser.add_argument("--output", "-o", default=None, help="Output file path. Defaults to stdout.")
        parser.add_argument(
            "--status",
            default=None,
            help="Filter by status (pending, confirmed, cancelled, ...).",
        )

    def handle(self, *args, **options):
        date_from = self._parse_date(options["date_from"]) or (timezone.localdate() - timedelta(days=30))
        date_to = self._parse_date(options["date_to"]) or timezone.localdate()

        queryset = Booking.objects.filter(check_in__gte=date_from, check_in__lte=date_to).select_related(
            "room", "room__room_type", "client"
        )
        if options["status"]:
            queryset = queryset.filter(status=options["status"])
        queryset = queryset.order_by("check_in", "reference")

        fieldnames = [
            "reference",
            "status",
            "room_type",
            "room",
            "check_in",
            "check_out",
            "nights",
            "adults",
            "children",
            "first_name",
            "last_name",
            "email",
            "phone",
            "total_price",
            "currency",
            "cancellation_reason",
            "cancellation_fee",
            "cancelled_by",
            "cancelled_at",
            "created_at",
            "confirmed_at",
        ]

        sink = open(options["output"], "w", newline="", encoding="utf-8") if options["output"] else sys.stdout
        try:
            writer = csv.writer(sink)
            writer.writerow(fieldnames)
            for booking in queryset:
                writer.writerow(
                    [
                        booking.reference,
                        booking.status,
                        booking.room.room_type.slug if booking.room else "",
                        booking.room.number if booking.room else "",
                        booking.check_in.isoformat(),
                        booking.check_out.isoformat(),
                        booking.nights,
                        booking.adults,
                        booking.children,
                        booking.client.first_name,
                        booking.client.last_name,
                        booking.client.email,
                        booking.client.phone,
                        str(booking.total_price),
                        booking.currency,
                        booking.cancellation_reason,
                        str(booking.cancellation_fee),
                        booking.cancelled_by,
                        booking.cancelled_at.isoformat() if booking.cancelled_at else "",
                        booking.created_at.isoformat(),
                        booking.confirmed_at.isoformat() if booking.confirmed_at else "",
                    ]
                )
        finally:
            if options["output"]:
                sink.close()

        if options["output"]:
            self.stdout.write(self.style.SUCCESS(f"Exported {queryset.count()} booking(s) to {options['output']}."))

    @staticmethod
    def _parse_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid date: {value}") from exc