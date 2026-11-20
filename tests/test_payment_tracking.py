"""Payment tracking foundation and payment summaries (v0.9.0 slices)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from rest_framework.test import APIClient

from apps.hotel.access import make_booking_token
from apps.hotel.booking_service import confirm_booking, create_booking
from apps.hotel.models import Booking
from apps.hotel.notifications import send_booking_confirmation, send_booking_confirmation_sms
from apps.hotel.sms_providers import ConsoleSmsProvider


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
    assert Decimal(str(payload["amount_due"])) == Decimal(str(payload["total_price"]))
    assert payload["payment_method"] == ""
    assert payload["payment_reference"] == ""
    assert payload["paid_at"] is None
    assert payload["payment_notes"] == ""

    detail = client.get(f"/api/v1/bookings/{payload['reference']}/", {"token": payload["manage_token"]})
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["payment_status"] == "unpaid"
    assert Decimal(str(detail_payload["amount_due"])) == Decimal(str(detail_payload["total_price"]))


@pytest.mark.django_db
def test_guest_booking_detail_shows_payment_summary(room_type, rooms, stay_dates):
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
    Booking.objects.filter(pk=booking.pk).update(
        payment_status=Booking.PaymentStatus.PARTIALLY_PAID,
        amount_paid=Decimal("100.00"),
    )
    booking.refresh_from_db()

    client = Client()
    token = make_booking_token(booking)
    response = client.get(f"/booking/{booking.reference}/", {"token": token}, HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Payment" in content
    assert "Partially paid" in content
    assert "Amount paid" in content
    assert "Balance due" in content
    assert "100.00" in content
    assert f"{booking.amount_due}" in content


@pytest.mark.django_db
def test_confirmation_messages_include_payment_summary(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48 510 378 422",
        adults=2,
        language="en",
    )
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])
    Booking.objects.filter(pk=booking.pk).update(
        payment_status=Booking.PaymentStatus.PARTIALLY_PAID,
        amount_paid=Decimal("100.00"),
    )
    booking.refresh_from_db()

    mail.outbox.clear()
    assert send_booking_confirmation(booking) is True
    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert "Payment status: Partially paid" in email.body
    assert f"Amount paid: {booking.amount_paid} {booking.currency}" in email.body
    assert f"Balance due: {booking.amount_due} {booking.currency}" in email.body
    assert email.alternatives
    html_body = email.alternatives[0][0]
    assert "Partially paid" in html_body
    assert f"{booking.amount_paid} {booking.currency}" in html_body
    assert f"{booking.amount_due} {booking.currency}" in html_body

    ConsoleSmsProvider.reset()
    assert send_booking_confirmation_sms(booking) is True
    assert ConsoleSmsProvider.outbox
    sms_body = ConsoleSmsProvider.outbox[-1].body
    assert "Payment status: Partially paid" in sms_body
    assert f"Amount paid: {booking.amount_paid} {booking.currency}" in sms_body
    assert f"Balance due: {booking.amount_due} {booking.currency}" in sms_body


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
