"""Signed booking access: tokens, contact lookup and revocation."""

from __future__ import annotations

import pytest

from apps.hotel.access import (
    BookingAccessError,
    booking_from_token,
    booking_manage_path,
    booking_manage_url,
    contact_matches,
    find_booking_for_contact,
    make_booking_token,
    token_matches,
)
from apps.hotel.booking_service import create_booking


@pytest.fixture
def booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    return create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48 600 100 200",
        adults=2,
    )


@pytest.mark.django_db
def test_token_round_trip_returns_the_booking(booking):
    token = make_booking_token(booking)

    assert booking_from_token(token) == booking
    assert token_matches(booking, token)


@pytest.mark.django_db
def test_token_is_scoped_to_one_booking(booking, room_type, stay_dates):
    check_in, check_out = stay_dates
    other = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Jan",
        last_name="Nowak",
        email="jan@example.com",
        adults=1,
    )

    assert token_matches(other, make_booking_token(booking)) is False


@pytest.mark.django_db
def test_garbage_and_empty_tokens_are_rejected(booking):
    assert token_matches(booking, "") is False
    assert token_matches(booking, "not-a-token") is False
    with pytest.raises(BookingAccessError):
        booking_from_token("not-a-token")


@pytest.mark.django_db
def test_rotating_access_token_revokes_existing_links(booking):
    token = make_booking_token(booking)

    booking.access_token = "a-brand-new-secret"
    booking.save(update_fields=["access_token"])

    assert token_matches(booking, token) is False


@pytest.mark.django_db
def test_manage_path_and_url_include_the_token(booking):
    path = booking_manage_path(booking)
    url = booking_manage_url(booking)

    assert path.startswith(f"/booking/{booking.reference}/")
    assert "token=" in path
    assert url.startswith("http")
    assert url.endswith(path)


@pytest.mark.django_db
def test_contact_matching_is_case_and_format_insensitive(booking):
    assert contact_matches(booking, "ANNA@example.com")
    assert contact_matches(booking, "+48 600 100 200")
    assert contact_matches(booking, "48600100200")
    assert contact_matches(booking, "someone@example.com") is False
    assert contact_matches(booking, "") is False


@pytest.mark.django_db
def test_contact_lookup_requires_both_parts(booking):
    assert find_booking_for_contact(booking.reference, "anna@example.com") == booking
    assert find_booking_for_contact(booking.reference, "nope@example.com") is None
    assert find_booking_for_contact("HA-NOPE00", "anna@example.com") is None

@pytest.mark.django_db
def test_contact_lookup_accepts_verified_linked_account_email(booking):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.hotel.models import GuestAccount

    user = get_user_model().objects.create_user(
        username="anna-account@example.com",
        email="anna-account@example.com",
        password="pw",
        is_active=True,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    booking.client.account = account
    booking.client.save(update_fields=["account", "updated_at"])

    # Preserved historical client contact still works.
    assert contact_matches(booking, "anna@example.com")
    # Current verified account email also works for linked bookings.
    assert contact_matches(booking, "anna-account@example.com")
    assert find_booking_for_contact(booking.reference, "anna-account@example.com") == booking


@pytest.mark.django_db
def test_contact_lookup_accepts_verified_account_associated_linked_client_email(booking):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.hotel.models import Client, GuestAccount

    user = get_user_model().objects.create_user(
        username="anna-account@example.com",
        email="anna-account@example.com",
        password="pw",
        is_active=True,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    booking.client.account = account
    booking.client.save(update_fields=["account", "updated_at"])

    Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="old-associated@example.com",
        phone="+48 500 000 001",
        account=account,
    )

    assert contact_matches(booking, "old-associated@example.com")
    assert find_booking_for_contact(booking.reference, "old-associated@example.com") == booking
    assert find_booking_for_contact(booking.reference, "unrelated@example.com") is None


@pytest.mark.django_db
def test_contact_lookup_accepts_verified_account_email_for_unlinked_associated_booking(booking):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.hotel.models import Client, GuestAccount

    user = get_user_model().objects.create_user(
        username="new-account@example.com",
        email="new-account@example.com",
        password="pw",
        is_active=True,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    # Booking client is unlinked, but its email is associated with the verified
    # account through another linked client record.
    booking.client.account = None
    booking.client.email = "old-associated@example.com"
    booking.client.save(update_fields=["account", "email", "updated_at"])

    Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="old-associated@example.com",
        phone="+48 500 000 002",
        account=account,
    )
    Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="other-associated@example.com",
        phone="+48 500 000 003",
        account=account,
    )

    assert contact_matches(booking, "new-account@example.com")
    assert contact_matches(booking, "old-associated@example.com")
    assert contact_matches(booking, "other-associated@example.com")
    assert find_booking_for_contact(booking.reference, "new-account@example.com") == booking
    assert find_booking_for_contact(booking.reference, "unrelated@example.com") is None


@pytest.mark.django_db
def test_contact_lookup_rejects_client_email_owned_by_different_verified_account(booking):
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.hotel.models import GuestAccount

    owner_user = get_user_model().objects.create_user(
        username="owner@example.com",
        email="owner@example.com",
        password="pw",
        is_active=True,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner_user)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])

    other_user = get_user_model().objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="pw",
        is_active=True,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])

    booking.client.account = owner_account
    booking.client.email = "other@example.com"  # cross-account conflict state
    booking.client.save(update_fields=["account", "email", "updated_at"])

    # other account owns that email via its user email; lookup must not leak owner booking.
    assert contact_matches(booking, "other@example.com") is False
    assert find_booking_for_contact(booking.reference, "other@example.com") is None
    # Owner account email still works.
    assert contact_matches(booking, "owner@example.com") is True
