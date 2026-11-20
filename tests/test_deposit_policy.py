"""Deposit/prepayment policy: calculation and guest-facing surfaces (v0.9.0)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core import mail
from django.test import Client
from rest_framework.test import APIClient

from apps.hotel.access import make_booking_token
from apps.hotel.booking_service import create_booking
from apps.hotel.deposit import deposit_summary
from apps.hotel.models import Booking
from apps.hotel.notifications import send_booking_confirmation, send_booking_confirmation_sms
from apps.hotel.sms_providers import ConsoleSmsProvider


@pytest.mark.django_db
def test_no_deposit_policy_means_balance_due_now(room_type, rooms, stay_dates, monkeypatch, settings):
    monkeypatch.setitem(settings.HOTEL, "deposit_policy", "none")
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

    summary = deposit_summary(booking)

    assert summary.policy == "none"
    assert summary.applies is False
    assert summary.deposit_amount == Decimal("0.00")
    assert summary.amount_due_now == booking.amount_due == booking.total_price
    assert "No deposit is required" in summary.policy_text


@pytest.mark.django_db
def test_percent_deposit_policy_calculates_due_now(room_type, rooms, stay_dates, monkeypatch, settings):
    monkeypatch.setitem(settings.HOTEL, "deposit_policy", "percent")
    monkeypatch.setitem(settings.HOTEL, "deposit_value", Decimal("30"))
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

    summary = deposit_summary(booking)
    expected_deposit = (booking.total_price * Decimal("30") / Decimal("100")).quantize(Decimal("0.01"))

    assert summary.policy == "percent"
    assert summary.applies is True
    assert summary.deposit_amount == expected_deposit
    assert summary.amount_due_now == expected_deposit
    assert "deposit" in summary.policy_text.lower()

    Booking.objects.filter(pk=booking.pk).update(amount_paid=Decimal("50.00"))
    booking.refresh_from_db()
    summary = deposit_summary(booking)
    assert summary.amount_due_now == (expected_deposit - Decimal("50.00")).quantize(Decimal("0.01"))
    assert summary.amount_due == (booking.total_price - Decimal("50.00")).quantize(Decimal("0.01"))


@pytest.mark.django_db
def test_fixed_deposit_policy_and_api_exposure(room_type, rooms, stay_dates, monkeypatch, settings):
    monkeypatch.setitem(settings.HOTEL, "deposit_policy", "fixed")
    monkeypatch.setitem(settings.HOTEL, "deposit_value", Decimal("200.00"))
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

    summary = deposit_summary(booking)
    assert summary.policy == "fixed"
    assert summary.deposit_amount == Decimal("200.00")
    assert summary.amount_due_now == Decimal("200.00")

    client = APIClient()
    token = make_booking_token(booking)
    response = client.get(f"/api/v1/bookings/{booking.reference}/", {"token": token})
    assert response.status_code == 200
    payload = response.json()
    assert Decimal(str(payload["deposit_amount"])) == Decimal("200.00")
    assert Decimal(str(payload["amount_due_now"])) == Decimal("200.00")
    assert payload["deposit"]["policy"] == "fixed"
    assert payload["deposit"]["applies"] is True
    assert Decimal(str(payload["deposit"]["deposit_amount"])) == Decimal("200.00")


@pytest.mark.django_db
def test_guest_booking_and_confirmations_show_deposit_summary(room_type, rooms, stay_dates, monkeypatch, settings):
    monkeypatch.setitem(settings.HOTEL, "deposit_policy", "percent")
    monkeypatch.setitem(settings.HOTEL, "deposit_value", Decimal("30"))
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Ola",
        last_name="Lis",
        email="ola@example.com",
        phone="+48 510 378 422",
        adults=2,
        language="en",
    )
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])

    token = make_booking_token(booking)
    client = Client()
    response = client.get(f"/booking/{booking.reference}/", {"token": token}, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Deposit required" in content
    assert "Amount due now" in content
    assert f"{booking.deposit_amount}" in content

    mail.outbox.clear()
    assert send_booking_confirmation(booking) is True
    email = mail.outbox[0]
    assert "Deposit required" in email.body
    assert "Amount due now" in email.body
    assert email.alternatives
    html_body = email.alternatives[0][0]
    assert "Deposit required" in html_body
    assert "Amount due now" in html_body

    ConsoleSmsProvider.reset()
    assert send_booking_confirmation_sms(booking) is True
    sms_body = ConsoleSmsProvider.outbox[-1].body
    assert "Deposit required" in sms_body
    assert "Amount due now" in sms_body
