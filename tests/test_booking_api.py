"""REST API: availability, pricing, booking lifecycle and error envelopes."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def _payload(room_type, stay_dates, **overrides):
    check_in, check_out = stay_dates
    data = {
        "room_type": room_type.slug,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "first_name": "Anna",
        "last_name": "Kowalska",
        "email": "anna@example.com",
        "adults": 2,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_health_endpoints(client):
    assert client.get("/healthz").status_code == 200
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.django_db
def test_room_types_endpoint(client, room_type, rooms, deluxe_suite, deluxe_room):
    response = client.get("/api/v1/room-types/")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    by_slug = {item["slug"]: item for item in payload}
    assert by_slug["standard-double"]["rooms_available"] == 2
    assert by_slug["deluxe-suite"]["rooms_available"] == 1


@pytest.mark.django_db
def test_availability_endpoint(client, room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    response = client.get(
        "/api/v1/availability/",
        {"check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "guests": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"]["guests"] == 2
    assert len(payload["results"]) == 1
    assert payload["results"][0]["available_rooms"] == 2


@pytest.mark.django_db
def test_pricing_endpoint(client, room_type, stay_dates):
    check_in, check_out = stay_dates
    response = client.get(
        "/api/v1/pricing/",
        {
            "room_type": room_type.slug,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "adults": 2,
        },
    )

    assert response.status_code == 200
    assert response.json()["quote"]["nights"] == 3
    assert response.json()["quote"]["total"] == "1200.00"


@pytest.mark.django_db
def test_booking_lifecycle(client, room_type, rooms, stay_dates):
    create = client.post("/api/v1/bookings/", _payload(room_type, stay_dates), format="json")
    assert create.status_code == 201
    booking = create.json()
    reference = booking["reference"]
    assert booking["status"] == "pending"
    assert booking["hold_expires_at"]

    detail = client.get(f"/api/v1/bookings/{reference}/")
    assert detail.status_code == 200
    assert detail.json()["reference"] == reference

    confirm = client.post(f"/api/v1/bookings/{reference}/confirm/")
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"

    cancel = client.post(f"/api/v1/bookings/{reference}/cancel/")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"


@pytest.mark.django_db
def test_booking_requires_contact(client, room_type, rooms, stay_dates):
    payload = _payload(room_type, stay_dates, email="", phone="")
    response = client.post("/api/v1/bookings/", payload, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert "email" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_booking_capacity_error(client, room_type, rooms, stay_dates):
    payload = _payload(room_type, stay_dates, adults=3)
    response = client.post("/api/v1/bookings/", payload, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "capacity_exceeded"


@pytest.mark.django_db
def test_booking_unknown_room_type(client, stay_dates):
    check_in, check_out = stay_dates
    payload = {
        "room_type": "missing",
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "first_name": "Anna",
        "email": "anna@example.com",
    }
    response = client.post("/api/v1/bookings/", payload, format="json")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "room_type_not_found"


@pytest.mark.django_db
def test_booking_not_found_envelope(client):
    response = client.get("/api/v1/bookings/HA-NOPE00/")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "booking_not_found"


@pytest.mark.django_db
def test_double_booking_is_rejected(client, room_type, stay_dates):
    # Only one room exists, so the second request cannot be fulfilled.
    from apps.hotel.models import Room

    Room.objects.create(number="101", room_type=room_type)

    first = client.post("/api/v1/bookings/", _payload(room_type, stay_dates), format="json")
    assert first.status_code == 201

    second = client.post("/api/v1/bookings/", _payload(room_type, stay_dates), format="json")
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "no_availability"
