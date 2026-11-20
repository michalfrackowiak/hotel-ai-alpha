"""Record a staff payment update for a booking and append an audit event."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from apps.hotel.booking_service import BookingError, record_booking_payment


class Command(BaseCommand):
    help = "Record a payment, adjustment, refund or waiver for a booking (append-only audit)."

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Booking reference, e.g. HA-ABC123")
        parser.add_argument(
            "--action",
            required=True,
            choices=["recorded", "adjusted", "refunded", "waived"],
            help="Type of payment change to record.",
        )
        parser.add_argument("--amount", help="Transaction amount (added for recorded, subtracted for refunded/waived, absolute for adjusted unless --amount-paid is set).")
        parser.add_argument("--amount-paid", dest="amount_paid", help="Resulting total amount paid (absolute).")
        parser.add_argument("--status", dest="payment_status", help="Resulting payment status (unpaid, partially_paid, paid, refunded).")
        parser.add_argument("--method", dest="payment_method", default=None, help="Payment method label.")
        parser.add_argument("--payment-reference", dest="payment_reference", default=None, help="External payment/reference number.")
        parser.add_argument("--notes", dest="payment_notes", default=None, help="Payment notes.")
        parser.add_argument("--paid-at", dest="paid_at", default=None, help="Payment timestamp (ISO 8601).")
        parser.add_argument("--summary", default="", help="Audit summary override.")
        parser.add_argument("--channel", default="admin", help="Audit channel (default: admin).")

    def handle(self, *args, **options):
        reference = (options["reference"] or "").strip().upper()

        def _decimal(name: str, raw: str | None) -> Decimal | None:
            if raw is None:
                return None
            try:
                return Decimal(raw)
            except (InvalidOperation, ValueError) as exc:
                raise CommandError(f"Invalid {name}: {raw}") from exc

        paid_at = None
        if options["paid_at"]:
            paid_at = parse_datetime(options["paid_at"])
            if paid_at is None:
                raise CommandError(f"Invalid --paid-at value: {options['paid_at']}")

        try:
            booking = record_booking_payment(
                reference,
                action=options["action"],
                amount=_decimal("--amount", options.get("amount")),
                amount_paid=_decimal("--amount-paid", options.get("amount_paid")),
                payment_status=options.get("payment_status"),
                payment_method=options.get("payment_method"),
                payment_reference=options.get("payment_reference"),
                paid_at=paid_at,
                payment_notes=options.get("payment_notes"),
                channel=options.get("channel") or "admin",
                summary=options.get("summary") or "",
            )
        except BookingError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded {options['action']} payment for {booking.reference}: "
                f"status={booking.payment_status} amount_paid={booking.amount_paid} "
                f"amount_due={booking.amount_due} {booking.currency}."
            )
        )
