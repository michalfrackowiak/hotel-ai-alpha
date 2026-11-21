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
from apps.hotel.guest_access import issue_email_verification_token, link_clients_for_guest_account
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
def test_email_change_form_rejects_same_email_and_neutral_for_unavailable_email(web_client):
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
    content = taken.content.decode().lower()
    # Anti-enumeration: unavailable/taken target gets the same neutral "sent"
    # response, but no token is issued and no verification email is sent.
    assert "verification link" in content
    assert "cannot be used" not in content
    assert GuestEmailVerificationToken.objects.filter(user=user).count() == 0
    assert mail.outbox == []


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
    assert GuestEmailVerificationToken.objects.filter(user=user, email="first@example.com").count() == 1

    second = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "second@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert second.status_code == 200
    # Cooldown prevents issuing/sending another token immediately, but the UI
    # response stays neutral/helpful and never errors for throttling.
    assert "verification link" in second.content.decode().lower()
    assert len(mail.outbox) == 1
    assert GuestEmailVerificationToken.objects.filter(user=user, email="second@example.com").count() == 0
    assert GuestEmailVerificationToken.objects.filter(user=user, email="first@example.com").count() == 1


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


@pytest.mark.django_db
def test_authenticated_guest_still_sees_account_linked_booking_after_email_change(
    web_client, room_type, rooms, stay_dates
):
    """Bookings linked to the verified account remain visible after a verified email change."""
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id

    assert web_client.login(username="anna@example.com", password="correct horse battery")
    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    response = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200

    user.refresh_from_db()
    booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert booking.client.account_id == account.id

    assert web_client.login(username="new-anna@example.com", password="correct horse battery")
    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert booking.reference in content
    assert "new-anna@example.com" in content

    detail = web_client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

@pytest.mark.django_db
def test_magic_link_session_still_accesses_account_bookings_after_verified_email_change(
    web_client, room_type, rooms, stay_dates
):
    """A magic-link session for the old email keeps access after email change."""
    from apps.hotel.guest_access import GUEST_SESSION_KEY

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id

    # Simulate an active magic-link session for the old (now-verified) email.
    session = web_client.session
    session[GUEST_SESSION_KEY] = "anna@example.com"
    session.save()

    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200

    user.refresh_from_db()
    assert user.email == "new-anna@example.com"

    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = web_client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_magic_link_can_be_requested_for_new_email_after_verified_email_change(
    web_client, room_type, rooms, stay_dates
):
    """Verified account email with linked bookings can request magic-link fallback after change."""
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id
    assert booking.client.email == "anna@example.com"

    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200

    user.refresh_from_db()
    assert user.email == "new-anna@example.com"

    response = web_client.post(
        "/my-bookings/",
        {"email": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    assert "sign-in link is on its way" in response.content.decode()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["new-anna@example.com"]

    path = re.search(r"/my-bookings/login/[^\s]+", mail.outbox[0].body).group(0)
    login = web_client.get(path)
    assert login.status_code == 302

    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert booking.reference in content
    assert "new-anna@example.com" in content

    detail = web_client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_email_change_preserves_linked_client_email_while_account_access_remains_coherent(
    web_client, room_type, rooms, stay_dates
):
    """A verified email change updates the account identity but does not rewrite
    historical linked-client contact data; account-linked access remains intact."""
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id
    assert booking.client.email == "anna@example.com"

    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    response = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200

    user.refresh_from_db()
    account.refresh_from_db()
    booking.client.refresh_from_db()

    assert user.email == "new-anna@example.com"
    assert account.is_email_verified is True
    # Historical client contact remains as recorded; linkage provides access.
    assert booking.client.account_id == account.id
    assert booking.client.email == "anna@example.com"

    assert web_client.login(username="new-anna@example.com", password="correct horse battery")
    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert booking.reference in content
    assert "new-anna@example.com" in content

    detail = web_client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_email_change_request_is_neutral_when_verify_ip_cooldown_blocks_issuance(web_client, settings):
    from apps.hotel.guest_access import _throttle_key

    settings.BOOKING_EMAILS_ENABLED = True
    user, _account = _create_verified_guest()
    web_client.force_login(user)
    cache.set(_throttle_key("verify-ip", "10.0.0.8"), 1, timeout=60)
    mail.outbox.clear()

    response = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.8",
    )

    assert response.status_code == 200
    assert "verification link" in response.content.decode().lower()
    assert len(mail.outbox) == 0
    assert GuestEmailVerificationToken.objects.filter(user=user, email="new-anna@example.com").count() == 0
    user.refresh_from_db()
    assert user.email == "anna@example.com"

@pytest.mark.django_db
def test_email_change_request_is_neutral_when_verify_email_cooldown_blocks_issuance(web_client, settings):
    from apps.hotel.guest_access import _throttle_key

    settings.BOOKING_EMAILS_ENABLED = True
    user, _account = _create_verified_guest()
    web_client.force_login(user)
    cache.set(_throttle_key("verify-email", str(user.pk)), 1, timeout=300)
    mail.outbox.clear()

    response = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.9",
    )

    assert response.status_code == 200
    assert "verification link" in response.content.decode().lower()
    assert len(mail.outbox) == 0
    assert GuestEmailVerificationToken.objects.filter(user=user, email="new-anna@example.com").count() == 0
    user.refresh_from_db()
    assert user.email == "anna@example.com"

@pytest.mark.django_db
def test_email_change_cooldown_preserves_pending_token_for_previous_target(web_client, settings):
    """If verification issuance is blocked after a successful request, the pending
    token for the previously requested target remains usable (no token rotation)."""
    settings.BOOKING_EMAILS_ENABLED = True
    user, _account = _create_verified_guest()
    web_client.force_login(user)

    first = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "first-new@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.20",
    )
    assert first.status_code == 200
    assert "verification link" in first.content.decode().lower()
    assert len(mail.outbox) == 1
    assert GuestEmailVerificationToken.objects.filter(user=user, email="first-new@example.com").count() == 1

    second = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "second-new@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.21",
    )
    assert second.status_code == 200
    assert "verification link" in second.content.decode().lower()
    # Cooldown blocks issuance/sending; no token rotation to the second target.
    assert len(mail.outbox) == 1
    assert GuestEmailVerificationToken.objects.filter(user=user).count() == 1
    assert GuestEmailVerificationToken.objects.filter(user=user, email="first-new@example.com").count() == 1
    assert GuestEmailVerificationToken.objects.filter(user=user, email="second-new@example.com").count() == 0

    user.refresh_from_db()
    assert user.email == "anna@example.com"

    match = re.search(r"(/my-bookings/verify-email/[^/]+/)", mail.outbox[0].body)
    assert match, mail.outbox[0].body
    verify = web_client.get(match.group(1), HTTP_ACCEPT_LANGUAGE="en")
    assert verify.status_code == 200
    user.refresh_from_db()
    assert user.email == "first-new@example.com"
    assert User.objects.filter(username="first-new@example.com", pk=user.pk).exists()
