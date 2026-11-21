"""Public website smoke tests."""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_home_page_renders(client):
    response = client.get("/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Hotel Aurora" in content
    assert "Check availability" in content


@pytest.mark.django_db
def test_home_page_renders_in_polish(client):
    response = client.get("/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    content = response.content.decode()
    assert 'lang="pl"' in content
    assert "Sprawdź dostępność" in content
    assert "Pokoje i rezerwacje" in content
    assert "Check availability" not in content


@pytest.mark.django_db
def test_home_page_lists_seeded_rooms(client):
    from django.core.management import call_command

    call_command("seed_demo")
    response = client.get("/")

    content = response.content.decode()
    assert "Standard Double" in content
    assert "Deluxe Suite" in content


@pytest.mark.django_db
def test_chat_and_voice_pages_render(client):
    assert client.get("/chat/").status_code == 200
    assert client.get("/voice/").status_code == 200


@pytest.mark.django_db
def test_booking_lookup_page_renders(client):
    response = client.get("/booking/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Find my booking" in content
    assert "Email or phone" in content


@pytest.mark.django_db
def test_guest_account_page_renders_in_both_languages(client):
    english = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    polish = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="pl")

    assert english.status_code == 200
    assert "My bookings" in english.content.decode()
    assert "Send sign-in link" in english.content.decode()
    assert polish.status_code == 200
    assert "Moje rezerwacje" in polish.content.decode()
    assert "Wyślij link do logowania" in polish.content.decode()


@pytest.mark.django_db
def test_unknown_booking_shows_not_found(client):
    response = client.get("/booking/HA-NOPE00/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 404
    assert "could not find" in response.content.decode().lower()


@pytest.mark.django_db
def test_booking_detail_requires_a_token(client):
    from django.core.management import call_command

    call_command("seed_demo")
    from apps.hotel.models import Booking

    booking = Booking.objects.get()
    response = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 403
    assert "invalid or has expired" in response.content.decode().lower()


@pytest.mark.django_db
def test_booking_detail_renders_seeded_booking(client):
    from django.core.management import call_command

    call_command("seed_demo")
    from apps.hotel.access import booking_manage_path
    from apps.hotel.models import Booking

    booking = Booking.objects.get()
    response = client.get(booking_manage_path(booking), HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert booking.reference in content
    assert "Confirmed" in content
    assert 'data-access-token="' in content


@pytest.mark.django_db
def test_booking_lookup_requires_matching_contact(client):
    from django.core.management import call_command

    call_command("seed_demo")
    from apps.hotel.models import Booking

    booking = Booking.objects.get()

    missing = client.get("/booking/", {"reference": booking.reference}, HTTP_ACCEPT_LANGUAGE="en")
    wrong = client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "someone@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    right = client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "anna.kowalska@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert missing.status_code == 200
    assert "protect your reservation" in missing.content.decode().lower()
    assert wrong.status_code == 200
    assert "could not match" in wrong.content.decode().lower()
    assert right.status_code == 302
    assert f"/booking/{booking.reference}/" in right["Location"]
    assert "token=" in right["Location"]


@pytest.mark.django_db
def test_booking_lookup_accepts_phone_number(client):
    from django.core.management import call_command

    call_command("seed_demo")
    from apps.hotel.models import Booking

    booking = Booking.objects.get()
    response = client.get(
        "/booking/", {"reference": booking.reference, "contact": "+48 600 100 200"}
    )

    assert response.status_code == 302


@pytest.mark.django_db
def test_booking_lookup_is_rate_limited_per_ip(client, settings):
    from django.core.cache import cache
    from django.core.management import call_command

    cache.clear()
    settings.GUEST_BOOKING_LOOKUP_WINDOW_SECONDS = 300
    settings.GUEST_BOOKING_LOOKUP_MAX_ATTEMPTS_PER_IP = 2

    call_command("seed_demo")
    from apps.hotel.models import Booking

    booking = Booking.objects.get()
    params = {"reference": booking.reference, "contact": "wrong@example.com"}

    first = client.get("/booking/", params, HTTP_ACCEPT_LANGUAGE="en")
    second = client.get("/booking/", params, HTTP_ACCEPT_LANGUAGE="en")
    third = client.get("/booking/", params, HTTP_ACCEPT_LANGUAGE="en")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    content = third.content.decode().lower()
    assert "too many attempts" in content
    assert "could not match" not in content


@pytest.mark.django_db
def test_phone_only_booking_can_be_looked_up_and_opened(client, room_type, rooms, stay_dates):
    from apps.hotel.booking_service import create_booking

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Marek",
        last_name="Nowak",
        email="",
        phone="+48 501 222 333",
        adults=1,
    )

    lookup = client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "+48 501 222 333"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert f"/booking/{booking.reference}/" in lookup["Location"]
    assert "token=" in lookup["Location"]

    detail = client.get(lookup["Location"], HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    content = detail.content.decode()
    assert booking.reference in content
    assert "+48 501 222 333" in content or "Marek" in content
    assert 'data-access-token="' in content


@pytest.mark.django_db
def test_healthz(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
