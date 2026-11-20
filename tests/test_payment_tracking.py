"""Payment tracking foundation (v0.9.0 slice): model fields, API exposure, ops visibility."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient

from apps.hotel.booking_service import confirm_booking, create_booking
from apps.hotel.models import Booking


@pytest.mark.django_db
def test_booking_api_exposes_payment_tracking_defaults(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    client = APIClient()
    response = client.post(
        "/api/v1/bookings/",
        {
            "room_type": room_type.slug,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "first_name": "Anna",
            "last_name": "Kowalska",
            "email": "anna@example.com",
            "adults": 2,
        },
        format="json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["payment_status"] == "unpaid"
    assert payload["payment_status_display"]
    assert Decimal(str(payload["amount_paid"])) == Decimal("0.00")
    assert payload["payment_method"] == ""
    assert payload["payment_reference"] == ""
    assert payload["paid_at"] is None
    assert payload["payment_notes"] == ""

    detail = client.get(f"/api/v1/bookings/{payload['reference']}/", {"token": payload["manage_token"]})
    assert detail.status_code == 200
    assert detail.json()["payment_status"] == "unpaid"


@pytest.mark.django_db
def test_ops_dashboard_surfaces_bookings_needing_payment(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Piotr",
        last_name="Nowak",
        email="piotr@example.com",
        adults=2,
    )
    confirm_booking(booking.reference)
    Booking.objects.filter(pk=booking.pk).update(
        payment_status=Booking.PaymentStatus.PARTIALLY_PAID,
        amount_paid=Decimal("100.00"),
    )

    user = get_user_model().objects.create_user(username="reception-payments", password="pw", is_staff=True)
    client = Client()
    client.force_login(user)
    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Bookings needing payment" in content
    assert booking.reference in content
    assert "Partially paid" in content
    assert "100.00" in content
