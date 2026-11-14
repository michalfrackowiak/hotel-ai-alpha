"""Guest notifications. Email failures must never roll back a reservation."""

from __future__ import annotations

import logging

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import override

from .access import booking_manage_url
from .email_providers import get_email_provider
from .models import Booking

logger = logging.getLogger(__name__)


def _context(booking: Booking) -> dict:
    return {
        "booking": booking,
        "hotel": settings.HOTEL,
        "manage_url": booking_manage_url(booking),
    }


def send_booking_confirmation(booking: Booking, *, force: bool = False) -> bool:
    """Send the bilingual confirmation email with an iCalendar attachment.

    Returns ``True`` when a message was handed to the email backend. The
    ``confirmation_email_sent_at`` marker makes retries idempotent.
    """
    if not settings.BOOKING_EMAILS_ENABLED:
        return False
    email = (booking.client.email or "").strip()
    if not email:
        return False
    if booking.confirmation_email_sent_at and not force:
        return False

    context = _context(booking)
    with override(booking.language or settings.HOTEL["default_language"]):
        subject = render_to_string("hotel/email/booking_confirmation_subject.txt", context).strip()
        text_body = render_to_string("hotel/email/booking_confirmation.txt", context)
        html_body = render_to_string("hotel/email/booking_confirmation.html", context)

    provider = get_email_provider()
    try:
        result = provider.send(booking, subject=subject, text_body=text_body, html_body=html_body)
    except Exception as exc:  # noqa: BLE001
        Booking.objects.filter(pk=booking.pk).update(
            confirmation_email_status="failed",
            confirmation_email_error=str(exc),
        )
        booking.confirmation_email_status = "failed"
        booking.confirmation_email_error = str(exc)
        raise

    if not result.sent:
        Booking.objects.filter(pk=booking.pk).update(
            confirmation_email_status="failed",
            confirmation_email_error=result.error,
        )
        booking.confirmation_email_status = "failed"
        booking.confirmation_email_error = result.error
        raise RuntimeError(result.error or "email delivery failed")

    sent_at = timezone.now()
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_email_sent_at=sent_at,
        confirmation_email_status="sent",
        confirmation_email_error="",
        confirmation_email_provider_id=result.provider_message_id,
    )
    booking.confirmation_email_sent_at = sent_at
    booking.confirmation_email_status = "sent"
    booking.confirmation_email_error = ""
    booking.confirmation_email_provider_id = result.provider_message_id
    logger.info("Sent confirmation email for booking %s to %s via %s", booking.reference, email, provider.name)
    return True


def send_pending_confirmation_emails(*, force: bool = False) -> int:
    """Retry confirmations that were confirmed before email delivery succeeded."""
    pending = Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN),
        client__email__gt="",
    )
    if not force:
        pending = pending.filter(confirmation_email_sent_at__isnull=True)
    pending = pending.select_related("room", "room__room_type", "client").order_by("pk")
    sent = 0
    for booking in pending:
        try:
            if send_booking_confirmation(booking, force=force):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the batch
            logger.exception("Confirmation email failed for booking %s", booking.reference)
    return sent
