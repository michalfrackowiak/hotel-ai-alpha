"""Payment tracking foundation and payment summaries (v0.9.0 slices)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client
from rest_framework.test import APIClient

from apps.hotel.access import make_booking_token
from apps.hotel.booking_service import BookingError, confirm_booking, create_booking, record_booking_payment
from apps.hotel.models import Booking, BookingEvent
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

@pytest.mark.django_db
def test_record_booking_payment_records_append_only_audit_event(room_type, rooms, stay_dates):
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
    assert booking.amount_paid == Decimal("0.00")
    assert booking.payment_status == Booking.PaymentStatus.UNPAID

    updated = record_booking_payment(
        booking.reference,
        action="recorded",
        amount=Decimal("100.00"),
        payment_method="card",
        payment_reference="PAY-100",
        payment_notes="Deposit",
    )

    updated.refresh_from_db()
    assert updated.amount_paid == Decimal("100.00")
    assert updated.payment_status == Booking.PaymentStatus.PARTIALLY_PAID
    assert updated.payment_method == "card"
    assert updated.payment_reference == "PAY-100"
    assert updated.payment_notes == "Deposit"
    assert updated.paid_at is not None

    event = updated.events.get(event_type=BookingEvent.Type.PAYMENT_RECORDED)
    assert event.actor == BookingEvent.Actor.STAFF
    assert event.channel == "admin"
    assert event.data["action"] == "recorded"
    assert event.data["before"]["amount_paid"] == "0.00"
    assert event.data["after"]["amount_paid"] == "100.00"
    assert event.data["delta"] == "100.00"

    with pytest.raises(ValueError):
        event.summary = "mutated"
        event.save()


@pytest.mark.django_db
def test_record_booking_payment_supports_adjust_refund_and_waive(room_type, rooms, stay_dates):
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

    record_booking_payment(booking.reference, action="recorded", amount=booking.total_price)
    booking.refresh_from_db()
    assert booking.payment_status == Booking.PaymentStatus.PAID
    assert booking.amount_due == Decimal("0.00")

    record_booking_payment(booking.reference, action="refunded", amount=Decimal("50.00"))
    booking.refresh_from_db()
    assert booking.amount_paid == (booking.total_price - Decimal("50.00")).quantize(Decimal("0.01"))
    assert booking.payment_status == Booking.PaymentStatus.PARTIALLY_PAID

    record_booking_payment(booking.reference, action="adjusted", amount_paid=Decimal("0.00"), payment_status=Booking.PaymentStatus.UNPAID)
    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("0.00")
    assert booking.payment_status == Booking.PaymentStatus.UNPAID

    record_booking_payment(booking.reference, action="waived", amount=Decimal("0.00"), amount_paid=booking.total_price, payment_status=Booking.PaymentStatus.PAID, payment_notes="Waived")
    booking.refresh_from_db()
    assert booking.payment_status == Booking.PaymentStatus.PAID
    assert booking.amount_paid == booking.total_price
    assert booking.payment_notes == "Waived"
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_RECORDED).count() == 1
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_REFUNDED).count() == 1
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_ADJUSTED).count() == 1
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_WAIVED).count() == 1


@pytest.mark.django_db
def test_record_booking_payment_validates_input(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Ola",
        last_name="Lis",
        email="ola@example.com",
        adults=2,
    )

    with pytest.raises(BookingError) as exc:
        record_booking_payment(booking.reference, action="bogus", amount=Decimal("10.00"))
    assert exc.value.code == "invalid_payment_action"

    with pytest.raises(BookingError) as exc:
        record_booking_payment(booking.reference, action="recorded")
    assert exc.value.code == "amount_required"

    with pytest.raises(BookingError) as exc:
        record_booking_payment(booking.reference, action="recorded", amount=Decimal("-1.00"))
    assert exc.value.code == "invalid_amount"

    with pytest.raises(BookingError) as exc:
        record_booking_payment("HA-NOPE00", action="recorded", amount=Decimal("10.00"))
    assert exc.value.code == "booking_not_found"


@pytest.mark.django_db
def test_record_payment_management_command(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Jan",
        last_name="Kowal",
        email="jan@example.com",
        adults=2,
    )

    call_command(
        "record_payment",
        booking.reference,
        action="recorded",
        amount="120.00",
        method="transfer",
        payment_reference="TR-1",
        notes="Bank transfer",
    )

    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("120.00")
    assert booking.payment_status == Booking.PaymentStatus.PARTIALLY_PAID
    assert booking.payment_method == "transfer"
    assert booking.payment_reference == "TR-1"
    assert booking.payment_notes == "Bank transfer"
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_RECORDED).exists()
