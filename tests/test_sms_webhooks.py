"""SMS delivery receipts, inbound replies and reception visibility."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib import admin
from django.utils import timezone

from apps.hotel.admin import InboundSmsMessageAdmin, SmsDeliveryAdmin
from apps.hotel.booking_service import create_booking
from apps.hotel.models import Booking, InboundSmsMessage, SmsDelivery
from apps.hotel.notifications import send_booking_confirmation_sms


@pytest.fixture
def booking(room_type, rooms, stay_dates):
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
    return booking


def _sms_delivery(booking, *, attempt: int, status: str, message_id: str = "", created_at=None):
    delivery = SmsDelivery.objects.create(
        booking=booking,
        attempt=attempt,
        status=status,
        provider="twilio",
        provider_message_id=message_id,
        recipient=booking.client.phone,
        body=f"Booking confirmed · {booking.reference}",
    )
    if created_at is not None:
        SmsDelivery.objects.filter(pk=delivery.pk).update(created_at=created_at)
        delivery.refresh_from_db()
    return delivery


@pytest.mark.django_db
def test_send_confirmation_sms_records_a_delivery_row(booking):
    assert send_booking_confirmation_sms(booking) is True

    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "sent"
    assert booking.confirmation_sms_attempts == 1
    delivery = SmsDelivery.objects.get(booking=booking)
    assert delivery.attempt == 1
    assert delivery.status == SmsDelivery.Status.SENT
    assert delivery.recipient == "+48 510 378 422"
    assert booking.reference in delivery.body
    assert delivery.provider == "console"


@pytest.mark.django_db
def test_failed_sms_send_records_a_failed_delivery_row(booking, monkeypatch):
    from apps.hotel import notifications
    from apps.hotel.sms_providers import SmsDeliveryResult

    class FailingProvider:
        name = "failing"

        def send(self, message):
            return SmsDeliveryResult(sent=False, error="carrier rejected")

    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingProvider())
    assert send_booking_confirmation_sms(booking) is False

    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "failed"
    assert booking.confirmation_sms_error == "carrier rejected"
    delivery = SmsDelivery.objects.get(booking=booking)
    assert delivery.status == SmsDelivery.Status.FAILED
    assert delivery.error_message == "carrier rejected"
    assert delivery.provider == "failing"


@pytest.mark.django_db
def test_twilio_delivery_receipt_marks_booking_and_delivery_delivered(booking, client):
    _sms_delivery(booking, attempt=1, status=SmsDelivery.Status.SENT, message_id="SM123")
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_status="sent", confirmation_sms_provider_id="SM123"
    )

    response = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM123", "MessageStatus": "delivered", "To": "+48510378422"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["sms_status"] == "delivered"
    booking.refresh_from_db()
    delivery = SmsDelivery.objects.get(booking=booking)
    assert booking.confirmation_sms_status == "delivered"
    assert booking.confirmation_sms_error == ""
    assert delivery.status == SmsDelivery.Status.DELIVERED


@pytest.mark.django_db
def test_twilio_failed_receipt_updates_latest_matching_delivery(booking, client):
    older = _sms_delivery(
        booking,
        attempt=1,
        status=SmsDelivery.Status.SENT,
        message_id="SM-old",
        created_at=timezone.now() - timedelta(minutes=1),
    )
    newer = _sms_delivery(booking, attempt=2, status=SmsDelivery.Status.SENT, message_id="SM-new")

    response = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM-new", "MessageStatus": "failed", "ErrorCode": "30003", "ErrorMessage": "Unreachable"},
        content_type="application/json",
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    older.refresh_from_db()
    newer.refresh_from_db()
    assert booking.confirmation_sms_status == "failed"
    assert "30003" in booking.confirmation_sms_error
    assert "Unreachable" in booking.confirmation_sms_error
    assert older.status == SmsDelivery.Status.SENT
    assert newer.status == SmsDelivery.Status.FAILED
    assert "Unreachable" in newer.error_message


@pytest.mark.django_db
def test_sms_webhook_requires_secret_when_configured(booking, client, settings):
    settings.SMS_WEBHOOK_SECRET = "topsecret"

    denied = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM123", "MessageStatus": "delivered"},
        content_type="application/json",
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM123", "MessageStatus": "delivered"},
        content_type="application/json",
        HTTP_X_SMS_WEBHOOK_SECRET="topsecret",
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ignored"


@pytest.mark.django_db
def test_unknown_sms_receipt_is_ignored(client):
    response = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM-does-not-exist", "MessageStatus": "delivered"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.django_db
def test_inbound_sms_reply_is_stored_and_matched_to_booking(booking, client):
    response = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM-in-1", "From": "+48 510 378 422", "To": "+48580000000", "Body": "Can I check in early?"},
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "inbound"
    assert payload["reference"] == booking.reference
    message = InboundSmsMessage.objects.get()
    assert message.booking == booking
    assert message.from_number == "+48 510 378 422"
    assert message.body == "Can I check in early?"
    assert message.provider == "twilio"


@pytest.mark.django_db
def test_unmatched_inbound_sms_reply_is_stored_without_booking(client):
    response = client.post(
        "/api/v1/sms/webhooks/twilio/",
        data={"MessageSid": "SM-in-2", "From": "+48 999 000 111", "To": "+48580000000", "Body": "Hello"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["reference"] == ""
    message = InboundSmsMessage.objects.get()
    assert message.booking is None
    assert message.body == "Hello"


@pytest.mark.django_db
def test_ops_dashboard_shows_sms_delivery_and_inbound_replies(booking, client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="reception", password="pw", is_staff=True)
    client.force_login(user)
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_status="failed",
        confirmation_sms_attempts=1,
        confirmation_sms_error="carrier rejected",
    )
    SmsDelivery.objects.create(
        booking=booking,
        attempt=1,
        status=SmsDelivery.Status.FAILED,
        provider="twilio",
        recipient=booking.client.phone,
        body="Booking confirmed",
        error_message="carrier rejected",
    )
    InboundSmsMessage.objects.create(
        booking=booking,
        from_number=booking.client.phone,
        to_number="+48580000000",
        body="Please confirm late arrival",
        provider="twilio",
    )

    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "SMS delivery" in content
    assert "SMS history" in content
    assert "Inbound SMS replies" in content
    assert booking.reference in content
    assert "carrier rejected" in content
    assert "Please confirm late arrival" in content


@pytest.mark.django_db
def test_ops_sms_sections_are_translated(client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="reception", password="pw", is_staff=True)
    client.force_login(user)

    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Dostarczanie SMS" in content
    assert "Historia SMS" in content
    assert "Przychodzące odpowiedzi SMS" in content


@pytest.mark.django_db
def test_sms_admin_models_are_read_only():
    delivery_admin = SmsDeliveryAdmin(SmsDelivery, admin.site)
    inbound_admin = InboundSmsMessageAdmin(InboundSmsMessage, admin.site)

    for model_admin in (delivery_admin, inbound_admin):
        assert model_admin.has_add_permission(None) is False
        assert model_admin.has_change_permission(None) is False
        assert model_admin.has_delete_permission(None) is False
