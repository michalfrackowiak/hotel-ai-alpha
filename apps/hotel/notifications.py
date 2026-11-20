"""Guest notifications. Email failures must never roll back a reservation."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import override

from .access import booking_manage_url
from .deposit import deposit_summary
from .email_providers import Attachment, EmailMessage, get_email_provider
from .ics import build_booking_ics
from .models import Booking, EmailDelivery, SmsDelivery
from .sms_providers import SmsMessage, get_sms_provider

logger = logging.getLogger(__name__)


def max_email_attempts() -> int:
    """How many delivery attempts a confirmation email gets before we stop."""
    return max(int(getattr(settings, "EMAIL_MAX_ATTEMPTS", 5)), 1)


def retry_delay_seconds(attempts: int) -> int:
    """Exponential backoff between attempts, capped by configuration."""
    base = max(int(getattr(settings, "EMAIL_RETRY_BASE_SECONDS", 300)), 1)
    cap = max(int(getattr(settings, "EMAIL_RETRY_MAX_SECONDS", 21600)), base)
    exponent = max(attempts - 1, 0)
    return min(base * (2**exponent), cap)


def max_sms_attempts() -> int:
    """How many delivery attempts a confirmation SMS gets before we stop."""
    return max(int(getattr(settings, "SMS_MAX_ATTEMPTS", 5)), 1)


def sms_retry_delay_seconds(attempts: int) -> int:
    """Exponential backoff between SMS attempts, capped by configuration."""
    base = max(int(getattr(settings, "SMS_RETRY_BASE_SECONDS", 300)), 1)
    cap = max(int(getattr(settings, "SMS_RETRY_MAX_SECONDS", 21600)), base)
    exponent = max(attempts - 1, 0)
    return min(base * (2**exponent), cap)


def _context(booking: Booking) -> dict:
    return {
        "booking": booking,
        "hotel": settings.HOTEL,
        "manage_url": booking_manage_url(booking),
        "deposit": deposit_summary(booking),
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

    message = EmailMessage(
        to=email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        attachments=[
            Attachment(
                filename=f"{booking.reference}.ics",
                content=build_booking_ics(booking).encode(),
                mimetype="text/calendar",
            )
        ],
        metadata={"booking_reference": booking.reference},
    )

    provider = get_email_provider()
    attempt_at = timezone.now()
    attempts = (booking.confirmation_email_attempts or 0) + 1
    result = None
    error = ""
    try:
        result = provider.send(message)
        if not result.sent:
            error = result.error or "email delivery failed"
    except Exception as exc:  # noqa: BLE001 - one failure must not break the booking
        error = str(exc)

    if error:
        next_retry_at = None
        if attempts < max_email_attempts():
            next_retry_at = attempt_at + timedelta(seconds=retry_delay_seconds(attempts))
        Booking.objects.filter(pk=booking.pk).update(
            confirmation_email_status="failed",
            confirmation_email_error=error,
            confirmation_email_attempts=attempts,
            confirmation_email_last_attempt_at=attempt_at,
            confirmation_email_next_retry_at=next_retry_at,
        )
        booking.confirmation_email_status = "failed"
        booking.confirmation_email_error = error
        booking.confirmation_email_attempts = attempts
        booking.confirmation_email_last_attempt_at = attempt_at
        booking.confirmation_email_next_retry_at = next_retry_at
        EmailDelivery.objects.create(
            booking=booking,
            attempt=attempts,
            status=EmailDelivery.Status.FAILED,
            provider=provider.name,
            recipient=email,
            subject=subject,
            error_message=error,
        )
        logger.warning(
            "Confirmation email attempt %s/%s failed for booking %s via %s: %s",
            attempts,
            max_email_attempts(),
            booking.reference,
            provider.name,
            error,
        )
        return False

    sent_at = attempt_at
    provider_id = result.provider_message_id if result else ""
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_email_sent_at=sent_at,
        confirmation_email_status="sent",
        confirmation_email_error="",
        confirmation_email_provider_id=provider_id,
        confirmation_email_attempts=attempts,
        confirmation_email_last_attempt_at=attempt_at,
        confirmation_email_next_retry_at=None,
    )
    booking.confirmation_email_sent_at = sent_at
    booking.confirmation_email_status = "sent"
    booking.confirmation_email_error = ""
    booking.confirmation_email_provider_id = provider_id
    booking.confirmation_email_attempts = attempts
    booking.confirmation_email_last_attempt_at = attempt_at
    booking.confirmation_email_next_retry_at = None
    EmailDelivery.objects.create(
        booking=booking,
        attempt=attempts,
        status=EmailDelivery.Status.SENT,
        provider=provider.name,
        provider_message_id=provider_id,
        recipient=email,
        subject=subject,
    )
    logger.info("Sent confirmation email for booking %s to %s via %s", booking.reference, email, provider.name)
    return True


def send_booking_confirmation_sms(booking: Booking, *, force: bool = False) -> bool:
    """Send a short confirmation SMS to the guest's phone number.

    Returns ``True`` when the provider accepted the message. The
    ``confirmation_sms_sent_at`` marker keeps retries idempotent. SMS delivery
    failures never roll back a reservation.
    """
    if not getattr(settings, "BOOKING_SMS_ENABLED", True):
        return False
    phone = (booking.client.phone or "").strip()
    if not phone:
        return False
    if booking.confirmation_sms_sent_at and not force:
        return False

    context = _context(booking)
    with override(booking.language or settings.HOTEL["default_language"]):
        body = render_to_string("hotel/sms/booking_confirmation.txt", context).strip()

    message = SmsMessage(
        to=phone,
        body=body,
        metadata={"booking_reference": booking.reference},
    )
    provider = get_sms_provider()
    attempt_at = timezone.now()
    attempts = (booking.confirmation_sms_attempts or 0) + 1
    result = None
    error = ""
    try:
        result = provider.send(message)
        if not result.sent:
            error = result.error or "SMS delivery failed"
    except Exception as exc:  # noqa: BLE001 - one failure must not break the booking
        error = str(exc)

    if error:
        next_retry_at = None
        if attempts < max_sms_attempts():
            next_retry_at = attempt_at + timedelta(seconds=sms_retry_delay_seconds(attempts))
        Booking.objects.filter(pk=booking.pk).update(
            confirmation_sms_status="failed",
            confirmation_sms_error=error,
            confirmation_sms_attempts=attempts,
            confirmation_sms_last_attempt_at=attempt_at,
            confirmation_sms_next_retry_at=next_retry_at,
        )
        booking.confirmation_sms_status = "failed"
        booking.confirmation_sms_error = error
        booking.confirmation_sms_attempts = attempts
        booking.confirmation_sms_last_attempt_at = attempt_at
        booking.confirmation_sms_next_retry_at = next_retry_at
        SmsDelivery.objects.create(
            booking=booking,
            attempt=attempts,
            status=SmsDelivery.Status.FAILED,
            provider=provider.name,
            recipient=phone,
            body=body,
            error_message=error,
        )
        logger.warning(
            "Confirmation SMS attempt %s/%s failed for booking %s via %s: %s",
            attempts,
            max_sms_attempts(),
            booking.reference,
            provider.name,
            error,
        )
        return False

    provider_id = result.provider_message_id if result else ""
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_sent_at=attempt_at,
        confirmation_sms_status="sent",
        confirmation_sms_error="",
        confirmation_sms_provider_id=provider_id,
        confirmation_sms_attempts=attempts,
        confirmation_sms_last_attempt_at=attempt_at,
        confirmation_sms_next_retry_at=None,
    )
    booking.confirmation_sms_sent_at = attempt_at
    booking.confirmation_sms_status = "sent"
    booking.confirmation_sms_error = ""
    booking.confirmation_sms_provider_id = provider_id
    booking.confirmation_sms_attempts = attempts
    booking.confirmation_sms_last_attempt_at = attempt_at
    booking.confirmation_sms_next_retry_at = None
    SmsDelivery.objects.create(
        booking=booking,
        attempt=attempts,
        status=SmsDelivery.Status.SENT,
        provider=provider.name,
        provider_message_id=provider_id,
        recipient=phone,
        body=body,
    )
    logger.info("Sent confirmation SMS for booking %s to %s via %s", booking.reference, phone, provider.name)
    return True


def send_guest_login_email(email: str, login_url: str, *, language: str = "en") -> bool:
    """Send a single-use sign-in link to the guest booking account.

    Delivery failures are logged but never surfaced to the caller: the request
    page always shows the same neutral "check your inbox" message so it cannot
    be used to discover which addresses have bookings.
    """
    if not settings.BOOKING_EMAILS_ENABLED:
        return False

    context = {"hotel": settings.HOTEL, "email": email, "login_url": login_url}
    with override(language or settings.HOTEL["default_language"]):
        subject = render_to_string("hotel/email/guest_login_subject.txt", context).strip()
        text_body = render_to_string("hotel/email/guest_login.txt", context)
        html_body = render_to_string("hotel/email/guest_login.html", context)

    message = EmailMessage(
        to=email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    provider = get_email_provider()
    try:
        result = provider.send(message)
    except Exception:  # noqa: BLE001
        logger.exception("Guest login email failed for %s", email)
        return False
    if not result.sent:
        logger.warning("Guest login email failed for %s via %s: %s", email, provider.name, result.error)
        return False
    logger.info("Sent guest login email to %s via %s", email, provider.name)
    return True


def send_pending_confirmation_emails(*, force: bool = False) -> int:
    """Retry confirmations that were confirmed before email delivery succeeded.

    Without ``force`` the retry queue honours the exponential backoff stored on
    each booking: a booking is only attempted again once ``next_retry_at`` has
    passed and it still has attempts left.
    """
    pending = Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN),
        client__email__gt="",
    )
    if not force:
        now = timezone.now()
        pending = (
            pending.filter(confirmation_email_sent_at__isnull=True)
            .filter(Q(confirmation_email_next_retry_at__isnull=True) | Q(confirmation_email_next_retry_at__lte=now))
            .filter(confirmation_email_attempts__lt=max_email_attempts())
        )
    pending = pending.select_related("room", "room__room_type", "client").order_by("pk")
    sent = 0
    for booking in pending:
        try:
            if send_booking_confirmation(booking, force=force):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the batch
            logger.exception("Confirmation email failed for booking %s", booking.reference)
    return sent


def send_pending_confirmation_sms(*, force: bool = False) -> int:
    """Retry confirmations that were confirmed before SMS delivery succeeded.

    Without ``force`` the retry queue honours the exponential backoff stored on
    each booking: a booking is only attempted again once ``next_retry_at`` has
    passed and it still has attempts left.
    """
    pending = Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN),
        client__phone__gt="",
    )
    if not force:
        now = timezone.now()
        pending = (
            pending.filter(confirmation_sms_sent_at__isnull=True)
            .filter(
                Q(confirmation_sms_next_retry_at__isnull=True)
                | Q(confirmation_sms_next_retry_at__lte=now)
            )
            .filter(confirmation_sms_attempts__lt=max_sms_attempts())
        )
    pending = pending.select_related("room", "room__room_type", "client").order_by("pk")
    sent = 0
    for booking in pending:
        try:
            if send_booking_confirmation_sms(booking, force=force):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad number must not stop the batch
            logger.exception("Confirmation SMS failed for booking %s", booking.reference)
    return sent
