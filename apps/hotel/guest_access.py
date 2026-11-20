"""Passwordless guest accounts.

A guest proves control of an email address by clicking a single-use link. The
link is stored only as a SHA-256 hash, expires quickly, and grants a session
that lists every booking tied to that address. Requests always get the same
neutral response, so the form cannot be used to discover who has a booking.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import Booking, GuestLoginToken

GUEST_SESSION_KEY = "guest_email"


def normalise_email(value: str | None) -> str:
    return (value or "").strip().lower()


def email_has_bookings(email: str) -> bool:
    email = normalise_email(email)
    if not email:
        return False
    return Booking.objects.filter(client__email__iexact=email).exists()


def bookings_for_email(email: str) -> QuerySet[Booking]:
    email = normalise_email(email)
    if not email:
        return Booking.objects.none()
    return (
        Booking.objects.filter(client__email__iexact=email)
        .select_related("client", "room", "room__room_type", "rate_plan")
        .order_by("-check_in", "-pk")
    )


def issue_login_token(email: str) -> tuple[GuestLoginToken, str] | None:
    """Create a login token for an address that has at least one booking."""
    email = normalise_email(email)
    if not email or not email_has_bookings(email):
        return None

    now = timezone.now()
    GuestLoginToken.objects.filter(email__iexact=email).filter(
        Q(expires_at__lte=now) | Q(used_at__isnull=False)
    ).delete()

    raw_token = secrets.token_urlsafe(32)
    record = GuestLoginToken.objects.create(
        email=email,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=settings.GUEST_LOGIN_MAX_AGE_MINUTES),
    )
    return record, raw_token


def consume_login_token(raw_token: str | None) -> str | None:
    """Mark a token as used and return its email address, or ``None``."""
    raw_token = (raw_token or "").strip()
    if not raw_token:
        return None

    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    with transaction.atomic():
        record = GuestLoginToken.objects.select_for_update().filter(token_hash=digest).first()
        if record is None or not record.is_usable:
            return None
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
    return normalise_email(record.email)


def session_email(request) -> str:
    return normalise_email(request.session.get(GUEST_SESSION_KEY))


def guest_session_matches(request, booking: Booking) -> bool:
    """True when the request is authenticated as the booking's guest owner."""
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False) and not getattr(user, "is_staff", False):
        user_email = normalise_email(getattr(user, "email", ""))
        if user_email and user_email == normalise_email(booking.client.email):
            return True
    email = session_email(request)
    return bool(email) and email == normalise_email(booking.client.email)


def _throttle_key(scope: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return f"guest-login:{scope}:{digest}"


def reserve_login_request(email: str, client_ip: str = "") -> bool:
    """Cache-based cooldown so the form cannot be used to bomb an inbox."""
    email = normalise_email(email)
    if not email:
        return False

    email_cooldown = int(getattr(settings, "GUEST_LOGIN_COOLDOWN_SECONDS", 300) or 0)
    if email_cooldown > 0 and not cache.add(
        _throttle_key("email", email), 1, timeout=email_cooldown
    ):
        return False

    ip_cooldown = int(getattr(settings, "GUEST_LOGIN_IP_COOLDOWN_SECONDS", 60) or 0)
    if client_ip and ip_cooldown > 0 and not cache.add(
        _throttle_key("ip", client_ip), 1, timeout=ip_cooldown
    ):
        return False
    return True
