"""Verified guest email-change flow."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking
from apps.hotel.guest_access import issue_email_verification_token
from apps.hotel.models import Client as HotelClient
from apps.hotel.models import GuestAccount, GuestEmailVerificationToken

User = get_user_model()


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def web_client() -> Client:
    return Client()


def _create_verified_guest(email="anna@example.com", password="correct horse battery"):
    user = User.objects.create_user(
        username=email,
        email=email,
        password=password,
        first_name="Anna",
        last_name="Kowalska",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])
    return user, account


@pytest.mark.django_db
def test_guest_can_request_email_change_and_verify_applies_it(web_client, settings):
    settings.BOOKING_EMAILS_ENABLED = True
    user, account = _create_verified_guest()
    linked = HotelClient.objects.create(
        account=account,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48111000000",
        language="pl",
    )
    web_client.force_login(user)

    response = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "verification link" in content.lower()
    assert "new-anna@example.com" in content

    user.refresh_from_db()
    assert user.email == "anna@example.com"
    assert user.username == "anna@example.com"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["new-anna@example.com"]
    assert "/my-bookings/verify-email/" in mail.outbox[0].body

    token = GuestEmailVerificationToken.objects.get(user=user, email="new-anna@example.com")
    assert token.used_at is None

    path = re.search(r"(/my-bookings/verify-email/[^/]+/)", mail.outbox[0].body).group(1)
    verify = web_client.get(path, HTTP_ACCEPT_LANGUAGE="en")
    assert verify.status_code == 200
    assert "verified" in verify.content.decode().lower()

    user.refresh_from_db()
    account.refresh_from_db()
    linked.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert user.username == "new-anna@example.com"
    assert user.is_active is True
    assert account.is_email_verified is True
    token.refresh_from_db()
    assert token.used_at is not None
    # Existing linked client record remains linked to the same account.
    assert linked.account_id == account.id

    profile = web_client.get("/my-bookings/profile/", HTTP_ACCEPT_LANGUAGE="en")
    assert profile.status_code == 200
    assert "new-anna@example.com" in profile.content.decode()

    web_client.logout()
    login = web_client.post(
        "/my-bookings/password-login/",
        {"username": "new-anna@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert login.status_code == 302
    old_login = web_client.post(
        "/my-bookings/password-login/",
        {"username": "anna@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert old_login.status_code == 400


@pytest.mark.django_db
def test_email_change_verification_links_bookings_for_new_verified_email(web_client, room_type, rooms, stay_dates):
    user, account = _create_verified_guest()
    web_client.force_login(user)

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="New",
        last_name="Owner",
        email="new-anna@example.com",
        adults=2,
    )
    assert booking.client.account_id is None

    web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    raw = None
    record = GuestEmailVerificationToken.objects.get(user=user, email="new-anna@example.com")
    # issue token directly to obtain raw value for deterministic verification
    record.delete()
    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")

    response = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id


@pytest.mark.django_db
def test_email_change_form_rejects_same_or_in_use_email(web_client):
    user, _account = _create_verified_guest()
    User.objects.create_user(
        username="taken@example.com",
        email="taken@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    web_client.force_login(user)

    same = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert same.status_code == 200
    assert "different email address" in same.content.decode().lower()
    assert GuestEmailVerificationToken.objects.filter(user=user).count() == 0

    taken = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "taken@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert taken.status_code == 200
    assert "cannot be used" in taken.content.decode().lower()
    assert GuestEmailVerificationToken.objects.filter(user=user).count() == 0


@pytest.mark.django_db
def test_email_change_consume_rejects_conflicting_target_email(web_client):
    user, _account = _create_verified_guest()
    _record, raw = issue_email_verification_token(user, email="conflict@example.com")
    # Another user claims the target before verification is completed.
    User.objects.create_user(
        username="conflict@example.com",
        email="conflict@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )

    response = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400
    assert "invalid" in response.content.decode().lower()

    user.refresh_from_db()
    assert user.email == "anna@example.com"
    assert user.username == "anna@example.com"


@pytest.mark.django_db
def test_email_change_token_invalid_expired_and_reused(web_client):
    user, _account = _create_verified_guest()

    invalid = web_client.get("/my-bookings/verify-email/not-a-real-token/", HTTP_ACCEPT_LANGUAGE="en")
    assert invalid.status_code == 400

    record, raw = issue_email_verification_token(user, email="new@example.com")
    record.expires_at = timezone.now() - timedelta(minutes=1)
    record.save(update_fields=["expires_at"])
    expired = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert expired.status_code == 400
    user.refresh_from_db()
    assert user.email == "anna@example.com"

    _record2, raw2 = issue_email_verification_token(user, email="fresh@example.com")
    first = web_client.get(f"/my-bookings/verify-email/{raw2}/", HTTP_ACCEPT_LANGUAGE="en")
    assert first.status_code == 200
    second = web_client.get(f"/my-bookings/verify-email/{raw2}/", HTTP_ACCEPT_LANGUAGE="en")
    assert second.status_code == 400


@pytest.mark.django_db
def test_email_change_cooldown_is_best_effort_without_revealing_block(web_client, settings):
    settings.BOOKING_EMAILS_ENABLED = True
    user, _account = _create_verified_guest()
    web_client.force_login(user)

    first = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "first@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert first.status_code == 200
    assert len(mail.outbox) == 1

    second = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "second@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert second.status_code == 200
    # Cooldown may prevent issuing/sending another token immediately, but the UI
    # response stays neutral/helpful and never errors for throttling.
    assert "verification link" in second.content.decode().lower()


@pytest.mark.django_db
def test_profile_email_change_renders_in_polish(web_client):
    user, _account = _create_verified_guest()
    web_client.force_login(user)

    response = web_client.get("/my-bookings/profile/", HTTP_ACCEPT_LANGUAGE="pl")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Zmień adres e-mail" in content
    assert "Nowy adres e-mail" in content
    assert "Wyślij link weryfikacyjny" in content
