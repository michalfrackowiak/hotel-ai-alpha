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

from .models import Booking, GuestAccount, GuestEmailVerificationToken, GuestLoginToken

GUEST_SESSION_KEY = "guest_email"


def normalise_email(value: str | None) -> str:
    return (value or "").strip().lower()


def email_has_bookings(email: str) -> bool:
    email = normalise_email(email)
    if not email:
        return False
    return Booking.objects.filter(client__email__iexact=email).exists()


def verified_guest_account_for_email(email: str) -> GuestAccount | None:
    email = normalise_email(email)
    if not email:
        return None
    return (
        GuestAccount.objects.select_related("user")
        .filter(
            email_verified_at__isnull=False,
            user__is_active=True,
            user__is_staff=False,
            user__email__iexact=email,
        )
        .first()
    )


def link_clients_for_guest_account(account: GuestAccount) -> int:
    """Link email-matching clients to a verified guest account.

    Only clients with no account or already linked to this account are updated,
    so an existing link to a different account is never overridden.
    """
    email = normalise_email(getattr(account, "email", "") or getattr(account.user, "email", ""))
    if not email or not account.is_email_verified:
        return 0
    return (
        Booking.objects.model._meta.apps.get_model("hotel", "Client")
        .objects.filter(email__iexact=email)
        .filter(Q(account__isnull=True) | Q(account=account))
        .exclude(account=account)
        .update(account=account)
    )


def bookings_for_email(email: str) -> QuerySet[Booking]:
    email = normalise_email(email)
    if not email:
        return Booking.objects.none()
    query = Q(client__email__iexact=email)
    account = verified_guest_account_for_email(email)
    if account is not None:
        query |= Q(client__account=account)
    return (
        Booking.objects.filter(query)
        .select_related("client", "room", "room__room_type", "rate_plan")
        .distinct()
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
        account = getattr(user, "guest_account", None)
        if (
            account is not None
            and getattr(account, "is_email_verified", False)
            and booking.client_id
            and booking.client.account_id == account.id
        ):
            return True
        user_email = normalise_email(getattr(user, "email", ""))
        if user_email and user_email == normalise_email(booking.client.email):
            return True
    email = session_email(request)
    if email and email == normalise_email(booking.client.email):
        return True
    # A magic-link session for the verified account email can also access
    # bookings linked to that account (without relying on client email match).
    if email:
        account = verified_guest_account_for_email(email)
        if account is not None and booking.client_id and booking.client.account_id == account.id:
            return True
    return False


def issue_email_verification_token(user, email: str | None = None) -> tuple[GuestEmailVerificationToken, str] | None:
    """Create a single-use email verification token for a guest user."""
    if user is None or getattr(user, "is_staff", False):
        return None
    email = normalise_email(email or getattr(user, "email", ""))
    if not email:
        return None

    now = timezone.now()
    GuestEmailVerificationToken.objects.filter(user=user).filter(
        Q(expires_at__lte=now) | Q(used_at__isnull=False)
    ).delete()

    raw_token = secrets.token_urlsafe(32)
    max_age_minutes = int(getattr(settings, "GUEST_EMAIL_VERIFICATION_MAX_AGE_MINUTES", 24 * 60) or 24 * 60)
    record = GuestEmailVerificationToken.objects.create(
        user=user,
        email=email,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=max_age_minutes),
    )
    return record, raw_token


def consume_email_verification_token(raw_token: str | None) -> str | None:
    """Mark a verification token used and return the verified email, or ``None``."""
    raw_token = (raw_token or "").strip()
    if not raw_token:
        return None

    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    with transaction.atomic():
        record = (
            GuestEmailVerificationToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=digest)
            .first()
        )
        if record is None or not record.is_usable:
            return None
        now = timezone.now()
        record.used_at = now
        record.save(update_fields=["used_at"])
        user = record.user
        email = normalise_email(record.email)
        if email and normalise_email(getattr(user, "email", "")) != email:
            user.email = email
            user.save(update_fields=["email"])
        account, _created = GuestAccount.objects.get_or_create(user=user)
        if account.email_verified_at is None:
            account.email_verified_at = now
            account.save(update_fields=["email_verified_at", "updated_at"])
        if not getattr(user, "is_active", False) and not getattr(user, "is_staff", False):
            user.is_active = True
            user.save(update_fields=["is_active"])
        # Link existing bookings/clients for this verified email to the account.
        link_clients_for_guest_account(account)
    return email


def can_issue_email_verification(request, user) -> bool:
    """Cache-based cooldown so verification emails cannot be spammed."""
    if user is None:
        return False
    email = normalise_email(getattr(user, "email", ""))
    if not email:
        return False
    email_cooldown = int(getattr(settings, "GUEST_EMAIL_VERIFICATION_COOLDOWN_SECONDS", 300) or 0)
    if email_cooldown > 0 and not cache.add(
        _throttle_key("verify-email", str(user.pk)), 1, timeout=email_cooldown
    ):
        return False
    client_ip = (getattr(request, "META", {}) or {}).get("REMOTE_ADDR", "")
    ip_cooldown = int(getattr(settings, "GUEST_LOGIN_IP_COOLDOWN_SECONDS", 60) or 0)
    if client_ip and ip_cooldown > 0 and not cache.add(
        _throttle_key("verify-ip", client_ip), 1, timeout=ip_cooldown
    ):
        return False
    return True


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
