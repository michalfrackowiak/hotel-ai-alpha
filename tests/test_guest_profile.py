"""Guest account profile/settings page."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.hotel.models import GuestAccount

User = get_user_model()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_guest_profile_requires_authenticated_non_staff(client):
    response = client.get("/my-bookings/profile/")
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")

    staff = User.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=True,
    )
    client.force_login(staff)
    response = client.get("/my-bookings/profile/")
    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")


@pytest.mark.django_db
def test_authenticated_guest_can_view_and_update_profile(client):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        first_name="Anna",
        last_name="Old",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])
    client.force_login(user)

    response = client.get("/my-bookings/profile/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Account settings" in content
    assert "anna@example.com" in content
    assert "Verified" in content
    assert "Anna" in content

    response = client.post(
        "/my-bookings/profile/",
        {"first_name": "Aneta", "last_name": "Kowalska"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 200
    assert "saved" in response.content.decode().lower()

    user.refresh_from_db()
    assert user.first_name == "Aneta"
    assert user.last_name == "Kowalska"

    # My bookings should offer account settings for authenticated guests
    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert "Account settings" in listing.content.decode()
