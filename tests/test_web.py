"""Public website smoke tests."""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_home_page_renders(client):
    response = client.get("/")

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
    response = client.get("/booking/")

    assert response.status_code == 200
    assert "Find my booking" in response.content.decode()


@pytest.mark.django_db
def test_unknown_booking_shows_not_found(client):
    response = client.get("/booking/HA-NOPE00/")

    assert response.status_code == 404
    assert "could not find" in response.content.decode().lower()


@pytest.mark.django_db
def test_booking_detail_renders_seeded_booking(client):
    from django.core.management import call_command

    call_command("seed_demo")
    from apps.hotel.models import Booking

    booking = Booking.objects.get()
    response = client.get(f"/booking/{booking.reference}/")

    assert response.status_code == 200
    content = response.content.decode()
    assert booking.reference in content
    assert "Confirmed" in content


@pytest.mark.django_db
def test_healthz(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
