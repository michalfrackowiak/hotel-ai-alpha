"""Verified guest account linkage to existing and future bookings/clients."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking, find_or_create_client
from apps.hotel.guest_access import consume_email_verification_token, issue_email_verification_token
from apps.hotel.models import Booking, Client as GuestClient, GuestAccount

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
def test_linking_does_not_override_different_account(room_type, rooms, stay_dates):
    # existing client linked to account A
    user_a = User.objects.create_user(
        username="a@example.com",
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

    # another verified account claims same email (edge case) should not steal link
    user_b = User.objects.create_user(
        username="b@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    account_b, _ = GuestAccount.objects.get_or_create(user=user_b)
    _record, raw = issue_email_verification_token(user_b)
    consume_email_verification_token(raw)

    existing.refresh_from_db()
    assert existing.account_id == account_a.id
    account_b.refresh_from_db()
    assert account_b.is_email_verified is True
