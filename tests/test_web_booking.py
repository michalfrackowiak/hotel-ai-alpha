"""Public web booking flow tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client

from apps.hotel.models import Booking, BookingEvent, RatePlan


@pytest.fixture
def client() -> Client:
    return Client()


def _stay_params(room_type, stay_dates) -> dict:
    check_in, check_out = stay_dates
    return {
        "room_type": room_type.slug,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
    }


@pytest.mark.django_db
def test_book_page_renders_start_form(client):
    response = client.get("/book/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Book your stay" in content
    assert "Check this stay" in content


@pytest.mark.django_db
def test_book_page_renders_in_polish(client):
    response = client.get("/book/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Zarezerwuj pobyt" in content
    assert "Sprawdź ten pobyt" in content


@pytest.mark.django_db
def test_home_page_links_to_direct_booking(client):
    response = client.get("/", HTTP_ACCEPT_LANGUAGE="en")

    content = response.content.decode()
    assert 'data-book-url="/book/"' in content
    assert "Book a room" in content


@pytest.mark.django_db
def test_book_page_shows_quote_and_rate_plans(client, room_type, rooms, stay_dates):
    RatePlan.objects.create(
        name="Non-refundable",
        slug="non-refundable",
        kind=RatePlan.Kind.PERCENT,
        adjustment=Decimal("-10.00"),
        is_refundable=False,
    )

    response = client.get(
        "/book/",
        _stay_params(room_type, stay_dates) | {"guests": 2},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "Standard rate" in content
    assert "Non-refundable" in content
    assert "Your details" in content


@pytest.mark.django_db
def test_book_post_creates_a_confirmed_web_booking(client, room_type, rooms, stay_dates):
    plan = RatePlan.objects.create(
        name="Non-refundable",
        slug="non-refundable",
        kind=RatePlan.Kind.PERCENT,
        adjustment=Decimal("-10.00"),
        is_refundable=False,
    )
    payload = _stay_params(room_type, stay_dates) | {
        "adults": 2,
        "children": 0,
        "rate_plan": plan.slug,
        "first_name": "Anna",
        "last_name": "Kowalska",
        "email": "anna@example.com",
        "phone": "",
        "special_requests": "High floor",
    }

    response = client.post("/book/", payload)

    assert response.status_code == 302
    booking = Booking.objects.get()
    assert booking.source == Booking.Source.WEB
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.rate_plan == plan
    assert booking.client.email == "anna@example.com"
    assert booking.special_requests == "High floor"
    assert f"/booking/{booking.reference}/" in response["Location"]
    assert "token=" in response["Location"]
    assert booking.events.filter(event_type=BookingEvent.Type.CREATED).exists()
    assert booking.events.filter(event_type=BookingEvent.Type.CONFIRMED).exists()
    assert any(
        "Rate plan" in rule
        for night in booking.price_breakdown["nightly"]
        for rule in night["rules"]
    )

    detail = client.get(response["Location"])
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_book_post_requires_contact(client, room_type, rooms, stay_dates):
    payload = _stay_params(room_type, stay_dates) | {
        "adults": 2,
        "children": 0,
        "first_name": "Anna",
        "last_name": "Kowalska",
        "email": "",
        "phone": "",
    }

    response = client.post("/book/", payload, HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    assert "Please provide an email address or a phone number." in response.content.decode()
    assert not Booking.objects.exists()


@pytest.mark.django_db
def test_book_page_rejects_invalid_dates(client, room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    response = client.get(
        "/book/",
        {
            "room_type": room_type.slug,
            "check_in": check_out.isoformat(),
            "check_out": check_in.isoformat(),
        },
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert "Check-out must be after check-in." in response.content.decode()
    assert not Booking.objects.exists()


@pytest.mark.django_db
def test_book_page_reports_no_availability(client, room_type, stay_dates):
    response = client.get(
        "/book/", _stay_params(room_type, stay_dates), HTTP_ACCEPT_LANGUAGE="en"
    )

    assert response.status_code == 200
    assert "No rooms are available for those dates." in response.content.decode()
