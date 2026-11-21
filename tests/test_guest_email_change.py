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

@pytest.mark.django_db
def test_reference_contact_lookup_still_works_with_preserved_client_email_after_email_change(
    web_client, room_type, rooms, stay_dates
):
    """Preserved historical client contact keeps the reference+contact fallback working."""
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
    booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert booking.client.email == "anna@example.com"

    lookup = web_client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert lookup["Location"].startswith(f"/booking/{booking.reference}/")
    assert "token=" in lookup["Location"]

    # Following the signed manage link opens the booking without re-entering contact.
    detail = web_client.get(lookup["Location"], HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_reference_contact_lookup_accepts_new_verified_account_email_after_email_change(
    web_client, room_type, rooms, stay_dates
):
    """Reference+contact lookup accepts the current verified account email after a verified email change."""
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
    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200

    user.refresh_from_db()
    booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert booking.client.email == "anna@example.com"
    assert booking.client.account_id == account.id

    lookup = web_client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "new-anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert lookup["Location"].startswith(f"/booking/{booking.reference}/")
    assert "token=" in lookup["Location"]

    detail = web_client.get(lookup["Location"], HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_magic_link_can_be_requested_for_preserved_client_email_after_verified_email_change(
    web_client, room_type, rooms, stay_dates
):
    """A preserved historical client email can still request a magic-link fallback
    and access account-linked bookings after a verified email change."""
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
    booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert booking.client.email == "anna@example.com"

    response = web_client.post(
        "/my-bookings/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    assert "sign-in link is on its way" in response.content.decode()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]

    path = re.search(r"/my-bookings/login/[^\s]+", mail.outbox[0].body).group(0)
    login = web_client.get(path)
    assert login.status_code == 302

    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = web_client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

@pytest.mark.django_db
def test_reference_contact_lookup_still_works_with_preserved_client_phone_after_email_change(
    web_client, room_type, rooms, stay_dates
):
    """Preserved client phone keeps the reference+contact fallback working after email change."""
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48 600 100 200",
        adults=2,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id
    assert booking.client.email == "anna@example.com"
    assert booking.client.phone == "+48 600 100 200"

    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200

    user.refresh_from_db()
    booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert booking.client.email == "anna@example.com"
    assert booking.client.phone == "+48 600 100 200"

    lookup = web_client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "+48 600 100 200"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert lookup["Location"].startswith(f"/booking/{booking.reference}/")
    assert "token=" in lookup["Location"]

    detail = web_client.get(lookup["Location"], HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

@pytest.mark.django_db
def test_register_with_preserved_historical_client_email_linked_to_other_verified_account_is_neutral(
    web_client, settings, room_type, rooms, stay_dates
):
    """Registration must not claim a preserved client email linked to another verified account."""
    settings.BOOKING_EMAILS_ENABLED = True
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=2,
    )

    owner, owner_account = _create_verified_guest(email="old@example.com")
    link_clients_for_guest_account(owner_account)
    booking.client.refresh_from_db()
    assert booking.client.account_id == owner_account.id

    # Owner changes to a new verified email; old client email remains preserved on the linked client.
    _record, raw = issue_email_verification_token(owner, email="new@example.com")
    response = web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    owner.refresh_from_db()
    booking.client.refresh_from_db()
    assert owner.email == "new@example.com"
    assert booking.client.email == "old@example.com"
    assert booking.client.account_id == owner_account.id

    mail.outbox.clear()
    register = web_client.post(
        "/my-bookings/register/",
        {
            "email": "old@example.com",
            "first_name": "Attacker",
            "last_name": "User",
            "password1": "correct horse battery",
            "password2": "correct horse battery",
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert register.status_code == 200
    assert "verification" in register.content.decode().lower()
    assert User.objects.filter(email__iexact="old@example.com").count() == 0
    assert mail.outbox == []
    booking.client.refresh_from_db()
    assert booking.client.account_id == owner_account.id


@pytest.mark.django_db
def test_email_change_to_preserved_historical_client_email_linked_to_other_verified_account_is_neutral(
    web_client, settings, room_type, rooms, stay_dates
):
    """Email-change requests must not target a preserved client email owned by another verified account."""
    settings.BOOKING_EMAILS_ENABLED = True
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=2,
    )

    owner, owner_account = _create_verified_guest(email="old@example.com")
    link_clients_for_guest_account(owner_account)
    _record, raw = issue_email_verification_token(owner, email="new@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200
    owner.refresh_from_db()
    booking.client.refresh_from_db()
    assert owner.email == "new@example.com"
    assert booking.client.email == "old@example.com"

    requester, _requester_account = _create_verified_guest(
        email="requester@example.com", password="correct horse battery"
    )
    web_client.force_login(requester)
    mail.outbox.clear()

    response = web_client.post(
        "/my-bookings/profile/",
        {"form": "email_change", "new_email": "old@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    content = response.content.decode().lower()
    assert "verification" in content
    assert mail.outbox == []
    assert not GuestEmailVerificationToken.objects.filter(
        user=requester, email__iexact="old@example.com", used_at__isnull=True
    ).exists()
    requester.refresh_from_db()
    assert requester.email == "requester@example.com"
    booking.client.refresh_from_db()
    assert booking.client.account_id == owner_account.id


@pytest.mark.django_db
def test_consume_email_verification_rejects_preserved_historical_client_email_linked_to_other_verified_account(
    web_client, room_type, rooms, stay_dates
):
    """Verification consumption backstop: do not verify an email owned by another verified account."""
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=2,
    )

    owner, owner_account = _create_verified_guest(email="old@example.com")
    link_clients_for_guest_account(owner_account)
    _record, raw = issue_email_verification_token(owner, email="new@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200
    owner.refresh_from_db()
    booking.client.refresh_from_db()
    assert owner.email == "new@example.com"
    assert booking.client.email == "old@example.com"

    attacker = User.objects.create_user(
        username="old@example.com",
        email="old@example.com",
        password="correct horse battery",
        first_name="Attacker",
        is_active=False,
        is_staff=False,
    )
    attacker_account, _ = GuestAccount.objects.get_or_create(user=attacker)
    _record2, raw2 = issue_email_verification_token(attacker, email="old@example.com")
    response = web_client.get(f"/my-bookings/verify-email/{raw2}/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400

    attacker.refresh_from_db()
    attacker_account.refresh_from_db()
    booking.client.refresh_from_db()
    assert attacker.is_active is False
    assert attacker_account.is_email_verified is False
    assert booking.client.account_id == owner_account.id

@pytest.mark.django_db
def test_unlinked_booking_with_preserved_account_email_is_visible_to_authenticated_guest(
    web_client, room_type, rooms, stay_dates
):
    """An unlinked booking whose client email is associated with a verified account
    (through another linked client) is visible/manageable to that authenticated guest."""
    from datetime import timedelta

    from apps.hotel.models import Booking, Client

    check_in, check_out = stay_dates
    # Linked booking/client with preserved historical email.
    linked_booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=1,
    )

    user, account = _create_verified_guest()
    link_clients_for_guest_account(account)
    _record, raw = issue_email_verification_token(user, email="new-anna@example.com")
    assert web_client.get(f"/my-bookings/verify-email/{raw}/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200

    user.refresh_from_db()
    linked_booking.client.refresh_from_db()
    assert user.email == "new-anna@example.com"
    assert linked_booking.client.account_id == account.id
    assert linked_booking.client.email == "anna@example.com"

    # Data-integrity edge case: another client record has the same preserved email
    # but is not linked to the account. Its booking should still be accessible to
    # the verified account owner because the email is associated with the account.
    unlinked_client = Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48555666777",
    )
    room = rooms[1]
    unlinked_booking = Booking.objects.create(
        client=unlinked_client,
        room=room,
        check_in=check_in + timedelta(days=30),
        check_out=check_out + timedelta(days=30),
        adults=1,
        children=0,
        status=Booking.Status.CONFIRMED,
        source=Booking.Source.WEB,
    )

    assert web_client.login(username="new-anna@example.com", password="correct horse battery")

    listing = web_client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert linked_booking.reference in content
    assert unlinked_booking.reference in content

    detail = web_client.get(f"/booking/{unlinked_booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert unlinked_booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_email_verification_change_handles_username_collision(web_client):
    # Another user occupies the target username but has a different email.
    User.objects.create_user(
        username="newname@example.com",
        email="other@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )

    user = User.objects.create_user(
        username="old@example.com",
        email="old@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    issued = issue_email_verification_token(user, email="newname@example.com")
    assert issued is not None
    _record, raw_token = issued

    response = web_client.get(f"/my-bookings/verify-email/{raw_token}/", follow=True, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200

    user.refresh_from_db()
    assert user.email == "newname@example.com"
    assert user.username.startswith("newname@example.com")
    assert user.username != "newname@example.com"

    # Fallback auth by email should still work.
    login = web_client.post(
        "/my-bookings/password-login/",
        {"username": "newname@example.com", "password": "correct horse battery"},
        follow=True,
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert login.status_code == 200
    assert login.wsgi_request.user.is_authenticated
