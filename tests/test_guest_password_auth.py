"""Guest password registration, email verification, reset and change flows."""

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
from apps.hotel.models import GuestAccount, GuestEmailVerificationToken

User = get_user_model()


@pytest.fixture(autouse=True)
def clear_login_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def anna_booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    return create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )


def _extract_path(message, prefix: str) -> str:
    match = re.search(rf"{re.escape(prefix)}[^\s\"<>]+", message.body)
    assert match, message.body
    return match.group(0)


@pytest.mark.django_db
def test_register_creates_inactive_guest_and_sends_verification(client, settings):
    settings.BOOKING_EMAILS_ENABLED = True

    response = client.post(
        "/my-bookings/register/",
        {
            "first_name": "Anna",
            "last_name": "Kowalska",
            "email": "Anna@Example.com",
            "password1": "correct horse battery",
            "password2": "correct horse battery",
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    content = response.content.decode().lower()
    assert "verification email is on its way" in content or "anna@example.com" in content

    user = User.objects.get(email="anna@example.com")
    assert user.is_active is False
    assert user.is_staff is False
    assert user.check_password("correct horse battery")
    assert GuestAccount.objects.filter(user=user).exists()
    assert user.guest_account.email_verified_at is None

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]
    assert "/my-bookings/verify-email/" in mail.outbox[0].body
    assert not client.session.get("_auth_user_id")


@pytest.mark.django_db
def test_register_rejects_invalid_password_and_does_not_create_user(client):
    response = client.post(
        "/my-bookings/register/",
        {
            "first_name": "Anna",
            "last_name": "Kowalska",
            "email": "anna@example.com",
            "password1": "short",
            "password2": "short",
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 400
    assert not User.objects.filter(email="anna@example.com").exists()


@pytest.mark.django_db
def test_verify_email_activates_user_and_marks_verified(client, settings):
    settings.BOOKING_EMAILS_ENABLED = True
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    record, raw = issue_email_verification_token(user)
    assert record is not None

    response = client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode().lower()
    assert "verified" in content

    user.refresh_from_db()
    assert user.is_active is True
    account = GuestAccount.objects.get(user=user)
    assert account.email_verified_at is not None
    token = GuestEmailVerificationToken.objects.get(pk=record.pk)
    assert token.used_at is not None


@pytest.mark.django_db
def test_verify_email_rejects_invalid_or_expired_token(client):
    response = client.get("/my-bookings/verify-email/not-a-real-token/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400
    assert "invalid" in response.content.decode().lower()

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    record, raw = issue_email_verification_token(user)
    record.expires_at = timezone.now() - timedelta(minutes=1)
    record.save(update_fields=["expires_at"])

    response = client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400


@pytest.mark.django_db
def test_verified_user_can_password_login_and_see_bookings(client, anna_booking):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    response = client.post(
        "/my-bookings/password-login/",
        {"username": "anna@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 302
    assert response["Location"] == "/my-bookings/"

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert anna_booking.reference in content
    assert "change password" in content.lower()


@pytest.mark.django_db
def test_inactive_unverified_user_cannot_password_login(client):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    response = client.post(
        "/my-bookings/password-login/",
        {"username": "anna@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 400
    assert not client.session.get("_auth_user_id")


@pytest.mark.django_db
def test_password_reset_request_for_active_guest_sends_email(client):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    response = client.post(
        "/my-bookings/password-reset/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/password-reset/done/")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]
    assert "/my-bookings/password-reset/" in mail.outbox[0].body


@pytest.mark.django_db
def test_password_reset_request_for_unknown_or_inactive_is_neutral(client):
    User.objects.create_user(
        username="inactive@example.com",
        email="inactive@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    mail.outbox.clear()

    for email in ("nobody@example.com", "inactive@example.com"):
        response = client.post(
            "/my-bookings/password-reset/",
            {"email": email},
            HTTP_ACCEPT_LANGUAGE="en",
        )
        assert response.status_code == 302
        assert response["Location"].endswith("/my-bookings/password-reset/done/")
    assert mail.outbox == []


@pytest.mark.django_db
def test_password_change_requires_auth_and_updates_password(client):
    # Unauthenticated redirects
    response = client.get("/my-bookings/password-change/")
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    client.force_login(user)

    response = client.get("/my-bookings/password-change/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    assert "current password" in response.content.decode().lower()

    response = client.post(
        "/my-bookings/password-change/",
        {
            "old_password": "correct horse battery",
            "new_password1": "even better passphrase",
            "new_password2": "even better passphrase",
        },
    )
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/password-change/done/")

    user.refresh_from_db()
    assert user.check_password("even better passphrase")
    # session should remain authenticated
    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200

@pytest.mark.django_db
def test_verify_email_token_cannot_be_reused(client):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)

    first = client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert first.status_code == 200

    second = client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert second.status_code == 400
    assert "invalid" in second.content.decode().lower()


@pytest.mark.django_db
def test_register_existing_verified_account_is_neutral_and_does_not_send(client, settings):
    settings.BOOKING_EMAILS_ENABLED = True
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])
    mail.outbox.clear()

    response = client.post(
        "/my-bookings/register/",
        {
            "first_name": "Anna",
            "last_name": "Kowalska",
            "email": "anna@example.com",
            "password1": "another strong passphrase",
            "password2": "another strong passphrase",
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert "verification email is on its way" in response.content.decode().lower()
    assert User.objects.filter(email="anna@example.com").count() == 1
    assert mail.outbox == []
    user.refresh_from_db()
    assert user.check_password("correct horse battery")


@pytest.mark.django_db
def test_staff_user_cannot_access_guest_password_change(client):
    staff = User.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=True,
    )
    client.force_login(staff)

    response = client.get("/my-bookings/password-change/")
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")


@pytest.mark.django_db
def test_password_reset_confirm_sets_new_password(client):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = f"/my-bookings/password-reset/{uid}/{token}/"

    # Django's confirm view may redirect to set-password form URL on GET
    response = client.get(url, follow=True, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200

    post_url = response.request["PATH_INFO"]
    response = client.post(
        post_url,
        {
            "new_password1": "brand new strong password",
            "new_password2": "brand new strong password",
        },
        follow=True,
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    assert "password updated" in response.content.decode().lower() or response.request["PATH_INFO"].endswith(
        "/my-bookings/password-reset/complete/"
    )
    user.refresh_from_db()
    assert user.check_password("brand new strong password")


@pytest.mark.django_db
def test_staff_cannot_password_login_via_guest_form(client):
    User.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=True,
    )

    response = client.post(
        "/my-bookings/password-login/",
        {"username": "staff@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 400
    assert not client.session.get("_auth_user_id")


@pytest.mark.django_db
def test_register_existing_staff_email_is_neutral_and_does_not_send(client, settings):
    settings.BOOKING_EMAILS_ENABLED = True
    staff = User.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=True,
    )
    mail.outbox.clear()

    response = client.post(
        "/my-bookings/register/",
        {
            "first_name": "Staff",
            "last_name": "User",
            "email": "staff@example.com",
            "password1": "another strong passphrase",
            "password2": "another strong passphrase",
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert "verification email is on its way" in response.content.decode().lower()
    assert mail.outbox == []
    staff.refresh_from_db()
    assert staff.is_staff is True
    assert staff.check_password("correct horse battery")


@pytest.mark.django_db
def test_password_change_done_requires_authenticated_non_staff(client):
    response = client.get("/my-bookings/password-change/done/")
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    client.force_login(user)
    ok = client.get("/my-bookings/password-change/done/", HTTP_ACCEPT_LANGUAGE="en")
    assert ok.status_code == 200

    staff = User.objects.create_user(
        username="staff2@example.com",
        email="staff2@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=True,
    )
    staff_client = Client()
    staff_client.force_login(staff)
    denied = staff_client.get("/my-bookings/password-change/done/")
    assert denied.status_code == 302
    assert denied["Location"].endswith("/my-bookings/")

@pytest.mark.django_db
def test_password_login_is_rate_limited_per_email_with_generic_error(client, settings):
    settings.GUEST_PASSWORD_LOGIN_WINDOW_SECONDS = 900
    settings.GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_EMAIL = 2
    settings.GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_IP = 100
    User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )

    url = "/my-bookings/password-login/"
    payload = {"username": "anna@example.com", "password": "wrong-password"}
    first = client.post(url, payload, HTTP_ACCEPT_LANGUAGE="en")
    second = client.post(url, payload, HTTP_ACCEPT_LANGUAGE="en")
    third = client.post(url, payload, HTTP_ACCEPT_LANGUAGE="en")

    assert first.status_code == 400
    assert second.status_code == 400
    assert third.status_code == 400
    assert not client.session.get("_auth_user_id")
    assert "please enter a correct" in third.content.decode().lower()


@pytest.mark.django_db
def test_password_login_rate_limit_does_not_authenticate_even_with_correct_password(client, settings):
    settings.GUEST_PASSWORD_LOGIN_WINDOW_SECONDS = 900
    settings.GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_EMAIL = 100
    settings.GUEST_PASSWORD_LOGIN_MAX_ATTEMPTS_PER_IP = 1
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    url = "/my-bookings/password-login/"
    first = client.post(
        url,
        {"username": "anna@example.com", "password": "wrong-password"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.9",
    )
    second = client.post(
        url,
        {"username": "anna@example.com", "password": "correct horse battery"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.9",
    )

    assert first.status_code == 400
    assert second.status_code == 400
    assert not client.session.get("_auth_user_id")
    assert "please enter a correct" in second.content.decode().lower()

@pytest.mark.django_db
def test_password_reset_request_is_rate_limited_but_response_stays_neutral(client, settings):
    settings.BOOKING_EMAILS_ENABLED = False
    # Ensure email backend captures locally in test environment.
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.GUEST_LOGIN_COOLDOWN_SECONDS = 300
    settings.GUEST_LOGIN_IP_COOLDOWN_SECONDS = 60
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    first = client.post(
        "/my-bookings/password-reset/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    second = client.post(
        "/my-bookings/password-reset/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert first["Location"].endswith("/my-bookings/password-reset/done/")
    assert second["Location"].endswith("/my-bookings/password-reset/done/")
    assert len(mail.outbox) == 1
