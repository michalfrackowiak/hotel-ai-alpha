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


@pytest.mark.django_db
def test_staff_payment_api_requires_staff_and_rejects_guest_token(room_type, rooms, stay_dates):
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
    url = f"/api/v1/bookings/{booking.reference}/payments/"
    payload = {"action": "recorded", "amount": "50.00"}

    client = APIClient()
    response = client.post(url, payload, format="json")
    assert response.status_code in {401, 403}

    # Guest booking token must not grant access to the staff-only path.
    guest = APIClient()
    guest.credentials(HTTP_X_BOOKING_TOKEN=make_booking_token(booking))
    guest_response = guest.post(url, payload, format="json")
    assert guest_response.status_code in {401, 403}
    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("0.00")
    assert not booking.events.filter(event_type=BookingEvent.Type.PAYMENT_RECORDED).exists()

    non_staff = get_user_model().objects.create_user(
        username="guest-user", password="pw", is_staff=False
    )
    user_client = APIClient()
    user_client.force_authenticate(user=non_staff)
    user_response = user_client.post(url, payload, format="json")
    assert user_response.status_code == 403


@pytest.mark.django_db
def test_staff_payment_api_records_payment_and_returns_updated_booking(room_type, rooms, stay_dates):
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
    staff = get_user_model().objects.create_user(username="staff-payments", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=staff)

    response = client.post(
        f"/api/v1/bookings/{booking.reference}/payments/",
        {
            "action": "recorded",
            "amount": "100.00",
            "status": "partially_paid",
            "method": "card",
            "payment_reference": "PAY-API-1",
            "notes": "Front desk",
            "paid_at": "2026-11-20T10:30:00Z",
            "summary": "Desk payment",
        },
        format="json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reference"] == booking.reference
    assert Decimal(str(payload["amount_paid"])) == Decimal("100.00")
    assert payload["payment_status"] == "partially_paid"
    assert payload["payment_method"] == "card"
    assert payload["payment_reference"] == "PAY-API-1"
    assert payload["payment_notes"] == "Front desk"
    assert payload["paid_at"] is not None
    assert Decimal(str(payload["amount_due"])) == (booking.total_price - Decimal("100.00")).quantize(
        Decimal("0.01")
    )

    booking.refresh_from_db()
    assert booking.amount_paid == Decimal("100.00")
    assert booking.payment_status == Booking.PaymentStatus.PARTIALLY_PAID
    assert booking.payment_method == "card"
    assert booking.payment_reference == "PAY-API-1"
    assert booking.payment_notes == "Front desk"
    assert booking.paid_at is not None
    event = booking.events.get(event_type=BookingEvent.Type.PAYMENT_RECORDED)
    assert event.actor == BookingEvent.Actor.STAFF
    assert event.channel == "api"
    assert event.summary == "Desk payment"
    assert event.data["action"] == "recorded"
    assert event.data["after"]["amount_paid"] == "100.00"


@pytest.mark.django_db
def test_staff_payment_api_supports_other_actions_and_validation_errors(room_type, rooms, stay_dates):
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
    staff = get_user_model().objects.create_user(username="staff-payments-2", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=staff)
    base_url = f"/api/v1/bookings/{booking.reference}/payments/"

    paid = client.post(
        base_url, {"action": "recorded", "amount": str(booking.total_price)}, format="json"
    )
    assert paid.status_code == 200
    assert Decimal(str(paid.json()["amount_due"])) == Decimal("0.00")

    refunded = client.post(base_url, {"action": "refunded", "amount": "50.00"}, format="json")
    assert refunded.status_code == 200
    assert refunded.json()["payment_status"] == "partially_paid"

    adjusted = client.post(
        base_url, {"action": "adjusted", "amount_paid": "0.00", "status": "unpaid"}, format="json"
    )
    assert adjusted.status_code == 200
    assert Decimal(str(adjusted.json()["amount_paid"])) == Decimal("0.00")
    assert adjusted.json()["payment_status"] == "unpaid"

    waived = client.post(base_url, {"action": "waived", "amount": "0.00"}, format="json")
    assert waived.status_code == 200
    assert waived.json()["payment_status"] == "unpaid"
    booking.refresh_from_db()
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_REFUNDED).exists()
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_ADJUSTED).exists()
    assert booking.events.filter(event_type=BookingEvent.Type.PAYMENT_WAIVED).exists()

    missing = client.post(base_url, {"action": "recorded"}, format="json")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "amount_required"

    invalid = client.post(base_url, {"action": "recorded", "amount": "-1.00"}, format="json")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] in {"validation_error", "invalid_amount"}

    not_found = client.post(
        "/api/v1/bookings/HA-NOPE00/payments/", {"action": "recorded", "amount": "10.00"}, format="json"
    )
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "booking_not_found"
