"""Signed guest access to bookings.

A booking reference is short and human-friendly, so it must never be the only
secret protecting a reservation. Every guest-facing read/confirm/cancel path
requires a signed token that contains the booking's random ``access_token``.
Tokens can be invalidated per booking by rotating that field.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse

from .models import Booking, GuestAccount

BOOKING_ACCESS_SALT = "hotel.booking.access"
_SIGNER = signing.TimestampSigner(salt=BOOKING_ACCESS_SALT)


class BookingAccessError(Exception):
    """The supplied token is missing, expired, forged or no longer valid."""


def make_booking_token(booking: Booking) -> str:
    return _SIGNER.sign_object(
        {"reference": booking.reference, "key": booking.access_token},
        compress=True,
    )


def booking_from_token(token: str) -> Booking:
    max_age_days = int(getattr(settings, "BOOKING_ACCESS_MAX_AGE_DAYS", 0) or 0)
    max_age = timedelta(days=max_age_days) if max_age_days > 0 else None
    try:
        payload = _SIGNER.unsign_object(token, max_age=max_age)
    except (signing.BadSignature, signing.SignatureExpired) as exc:
        raise BookingAccessError("The booking link is invalid or has expired.") from exc

    reference = str(payload.get("reference") or "").upper()
    key = str(payload.get("key") or "")
    booking = (
        Booking.objects.select_related("room", "room__room_type", "client")
        .filter(reference=reference)
        .first()
    )
    if booking is None or not secrets.compare_digest(booking.access_token, key):
        raise BookingAccessError("The booking link is invalid or has expired.")
    return booking


def token_matches(booking: Booking, token: str | None) -> bool:
    if not token:
        return False
    try:
        return booking_from_token(token).pk == booking.pk
    except BookingAccessError:
        return False


def booking_manage_path(booking: Booking) -> str:
    path = reverse("web:booking-detail", args=[booking.reference])
    return f"{path}?{urlencode({'token': make_booking_token(booking)})}"


def booking_manage_url(booking: Booking) -> str:
    base = settings.HOTEL["public_url"].rstrip("/")
    return f"{base}{booking_manage_path(booking)}"


def _normalise_phone(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def contact_matches(booking: Booking, contact: str) -> bool:
    """Compare an email address or phone number without leaking which one failed."""
    contact = (contact or "").strip().lower()
    if not contact:
        return False

    email = (booking.client.email or "").strip().lower()
    if email and secrets.compare_digest(email, contact):
        return True

    account = getattr(booking.client, "account", None)
    if account is None and booking.client_id and booking.client.account_id:
        account = (
            GuestAccount.objects.select_related("user")
            .filter(pk=booking.client.account_id, email_verified_at__isnull=False)
            .first()
        )
    if (
        account is not None
        and getattr(account, "is_email_verified", False)
        and getattr(getattr(account, "user", None), "is_active", False)
        and not getattr(getattr(account, "user", None), "is_staff", False)
    ):
        account_email = (getattr(account, "email", "") or "").strip().lower()
        if account_email and secrets.compare_digest(account_email, contact):
            return True
        # Accept any email associated with the same verified account (e.g.
        # preserved historical linked-client email on another client record).
        from apps.hotel.guest_access import verified_guest_account_for_email

        contact_account = verified_guest_account_for_email(contact)
        if contact_account is not None and contact_account.pk == account.pk:
            return True

    phone = _normalise_phone(booking.client.phone or "")
    supplied_phone = _normalise_phone(contact)
    if phone and supplied_phone and len(supplied_phone) >= 6:
        return secrets.compare_digest(phone, supplied_phone)
    return False


def find_booking_for_contact(reference: str, contact: str) -> Booking | None:
    booking = (
        Booking.objects.select_related("room", "room__room_type", "client", "client__account__user")
        .filter(reference=(reference or "").strip().upper())
        .first()
    )
    if booking is None or not contact_matches(booking, contact):
        return None
    return booking
