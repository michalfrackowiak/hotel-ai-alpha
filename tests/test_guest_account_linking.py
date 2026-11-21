"""Verified guest account linkage to existing and future bookings/clients."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking
from apps.hotel.guest_access import (
    consume_email_verification_token,
    issue_email_verification_token,
    link_clients_for_guest_account,
)
from apps.hotel.models import Client as GuestClient
from apps.hotel.models import GuestAccount

User = get_user_model()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_verifying_email_links_existing_clients(room_type, rooms, stay_dates):
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
    assert booking.client.account_id is None

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)

    verified_email = consume_email_verification_token(raw)
    assert verified_email == "anna@example.com"

    booking.client.refresh_from_db()
    account = GuestAccount.objects.get(user=user)
    assert booking.client.account_id == account.id
    assert account.is_email_verified is True


@pytest.mark.django_db
def test_future_booking_links_to_verified_guest_account(room_type, rooms, stay_dates):
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

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="ANNA@example.com",
        adults=2,
    )

    assert booking.client.account_id == account.id


@pytest.mark.django_db
def test_verified_authenticated_guest_sees_and_can_open_linked_booking(client, room_type, rooms, stay_dates):
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
    # simulate pre-existing unlinked booking, then verify/link
    booking.client.account = None
    booking.client.save(update_fields=["account", "updated_at"])

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)
    consume_email_verification_token(raw)

    user.refresh_from_db()
    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_linking_rejects_email_owned_by_different_user():
    # existing client linked to account A and its user already owns the email
    user_a = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_a, _ = GuestAccount.objects.get_or_create(user=user_a)
    account_a.email_verified_at = timezone.now()
    account_a.save(update_fields=["email_verified_at", "updated_at"])

    existing = GuestClient.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48111222333",
        account=account_a,
    )

    # another account must not be able to verify/claim an email owned by user A
    user_b = User.objects.create_user(
        username="b@example.com",
        email="b@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    account_b, _ = GuestAccount.objects.get_or_create(user=user_b)
    _record, raw = issue_email_verification_token(user_b, email="anna@example.com")
    verified_email = consume_email_verification_token(raw)

    assert verified_email is None
    existing.refresh_from_db()
    assert existing.account_id == account_a.id
    account_b.refresh_from_db()
    assert account_b.is_email_verified is False
    user_b.refresh_from_db()
    assert user_b.email == "b@example.com"


@pytest.mark.django_db
def test_pre_account_booking_becomes_visible_after_register_verify_and_magic_link_login(
    client, room_type, rooms, stay_dates, settings
):
    """A pre-account booking is linked on verification and visible via magic-link sign-in."""
    settings.BOOKING_EMAILS_ENABLED = True
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
    assert booking.client.account_id is None

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)
    assert consume_email_verification_token(raw) == "anna@example.com"

    booking.client.refresh_from_db()
    assert booking.client.account_id is not None

    response = client.post("/my-bookings/", {"email": "anna@example.com"}, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    match = __import__("re").search(r"/my-bookings/login/[^\s]+", mail.outbox[0].body)
    assert match, mail.outbox[0].body

    login = client.get(match.group(0))
    assert login.status_code == 302

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()



@pytest.mark.django_db
def test_link_clients_for_guest_account_does_not_override_different_account():
    """Safety: matching clients already linked to another account must not be stolen."""
    user_a = User.objects.create_user(
        username="a@example.com",
        email="a@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_a, _ = GuestAccount.objects.get_or_create(user=user_a)
    account_a.email_verified_at = timezone.now()
    account_a.save(update_fields=["email_verified_at", "updated_at"])

    shared_client = GuestClient.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="shared@example.com",
        phone="+48111222333",
        account=account_a,
    )

    # Account B is verified but its contact resolves to the same client email in this
    # data-integrity edge case; linking must not override account A.
    user_b = User.objects.create_user(
        username="b@example.com",
        email="shared@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_b, _ = GuestAccount.objects.get_or_create(user=user_b)
    account_b.email_verified_at = timezone.now()
    account_b.save(update_fields=["email_verified_at", "updated_at"])

    linked = link_clients_for_guest_account(account_b)

    assert linked == 0
    shared_client.refresh_from_db()
    assert shared_client.account_id == account_a.id



@pytest.mark.django_db
def test_future_booking_after_verification_is_visible_to_authenticated_guest(client, room_type, rooms, stay_dates):
    """Bookings created after account verification are linked and visible in My bookings."""
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
    assert booking.client.account_id == account.id

    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

@pytest.mark.django_db
def test_phone_only_client_is_linked_when_later_booking_provides_verified_email(
    client, room_type, rooms, stay_dates
):
    """A phone-only client/booking becomes account-linked when a later booking provides
    the same phone and a verified account email."""
    from datetime import timedelta

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

    check_in, check_out = stay_dates
    phone_only = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="",
        phone="+48 501 222 333",
        adults=1,
    )
    phone_only.client.refresh_from_db()
    assert phone_only.client.account_id is None
    assert phone_only.client.email == ""

    # Later booking with same phone provides the verified account email.
    later = create_booking(
        room_type=room_type,
        check_in=check_in + timedelta(days=30),
        check_out=check_out + timedelta(days=30),
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48 501 222 333",
        adults=1,
    )
    phone_only.client.refresh_from_db()
    later.client.refresh_from_db()
    assert later.client.pk == phone_only.client.pk
    assert phone_only.client.account_id == account.id
    assert phone_only.client.email == "anna@example.com"

    assert client.login(username="anna@example.com", password="correct horse battery")
    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert phone_only.reference in content
    assert later.reference in content

@pytest.mark.django_db
def test_phone_only_booking_linked_to_verified_account_is_visible_and_manageable(
    client, room_type, rooms, stay_dates
):
    """A phone-only booking linked to a verified account is visible in My bookings
    and accessible via the authenticated guest session."""
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

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="",
        phone="+48 501 222 333",
        adults=1,
    )
    # Link the phone-only client to the verified account explicitly.
    booking.client.account = account
    booking.client.save(update_fields=["account", "updated_at"])

    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

    # Reference+phone fallback remains available.
    client.logout()
    lookup = client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "+48 501 222 333"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert "token=" in lookup["Location"]
