"""Transactional email hardening: retry backoff, provider webhooks, dashboard."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.hotel import notifications
from apps.hotel.booking_service import create_booking
from apps.hotel.email_providers import DeliveryResult
from apps.hotel.models import Booking
from apps.hotel.notifications import (
    max_email_attempts,
    retry_delay_seconds,
    send_booking_confirmation,
    send_pending_confirmation_emails,
)


class FailingProvider:
    name = "failing"

    def send(self, message):
        return DeliveryResult(sent=False, error="smtp rejected the message")


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
        adults=2,
        language="en",
    )
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])
    return booking


def test_retry_delay_doubles_and_is_capped(settings):
    settings.EMAIL_RETRY_BASE_SECONDS = 60
    settings.EMAIL_RETRY_MAX_SECONDS = 200

    assert [retry_delay_seconds(n) for n in (1, 2, 3, 4)] == [60, 120, 200, 200]


@pytest.mark.django_db
def test_failed_send_schedules_a_retry(booking, monkeypatch, settings):
    settings.EMAIL_RETRY_BASE_SECONDS = 60
    monkeypatch.setattr(notifications, "get_email_provider", lambda: FailingProvider())

    assert send_booking_confirmation(booking) is False

    booking.refresh_from_db()
    assert booking.confirmation_email_status == "failed"
    assert booking.confirmation_email_error == "smtp rejected the message"
    assert booking.confirmation_email_attempts == 1
    assert booking.confirmation_email_last_attempt_at is not None
    assert booking.confirmation_email_next_retry_at is not None
    delay = booking.confirmation_email_next_retry_at - booking.confirmation_email_last_attempt_at
    assert delay == timedelta(seconds=60)


@pytest.mark.django_db
def test_successful_send_clears_the_retry_schedule(booking):
    assert send_booking_confirmation(booking) is True

    booking.refresh_from_db()
    assert booking.confirmation_email_status == "sent"
    assert booking.confirmation_email_attempts == 1
    assert booking.confirmation_email_last_attempt_at is not None
    assert booking.confirmation_email_next_retry_at is None
    assert booking.confirmation_email_error == ""


@pytest.mark.django_db
def test_retry_waits_for_the_backoff_window(booking, monkeypatch, settings):
    settings.EMAIL_RETRY_BASE_SECONDS = 300
    monkeypatch.setattr(notifications, "get_email_provider", lambda: FailingProvider())
    assert send_booking_confirmation(booking) is False
    mail.outbox.clear()

    # Still inside the backoff window: the retry queue must leave it alone.
    assert send_pending_confirmation_emails() == 0
    assert mail.outbox == []

    Booking.objects.filter(pk=booking.pk).update(
        confirmation_email_next_retry_at=timezone.now() - timedelta(seconds=1)
    )
    monkeypatch.undo()

    assert send_pending_confirmation_emails() == 1
    assert len(mail.outbox) == 1
    booking.refresh_from_db()
    assert booking.confirmation_email_status == "sent"
    assert booking.confirmation_email_attempts == 2


@pytest.mark.django_db
def test_retry_stops_at_the_attempt_cap(booking, monkeypatch, settings):
    settings.EMAIL_MAX_ATTEMPTS = 2
    monkeypatch.setattr(notifications, "get_email_provider", lambda: FailingProvider())
    assert max_email_attempts() == 2

    assert send_booking_confirmation(booking) is False
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_email_next_retry_at=timezone.now() - timedelta(seconds=1)
    )

    # The second attempt runs and fails; there is no third attempt once the cap
    # is reached, even though the retry queue keeps running.
    send_pending_confirmation_emails()
    booking.refresh_from_db()
    assert booking.confirmation_email_attempts == 2
    assert booking.confirmation_email_next_retry_at is None

    send_pending_confirmation_emails()
    booking.refresh_from_db()
    assert booking.confirmation_email_attempts == 2


@pytest.mark.django_db
def test_ses_bounce_webhook_matches_by_message_id(booking, client):
    booking.confirmation_email_status = "sent"
    booking.confirmation_email_provider_id = "ses-message-1"
    booking.save(update_fields=["confirmation_email_status", "confirmation_email_provider_id"])

    payload = {
        "Type": "Notification",
        "Message": json.dumps(
            {
                "notificationType": "Bounce",
                "mail": {"messageId": "ses-message-1"},
                "bounce": {
                    "bounceType": "Permanent",
                    "bouncedRecipients": [{"diagnosticCode": "550 mailbox unavailable"}],
                },
            }
        ),
    }
    response = client.post(
        "/api/v1/email/webhooks/ses/", payload, content_type="application/json"
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.confirmation_email_status == "bounced"
    assert booking.confirmation_email_error == "550 mailbox unavailable"


@pytest.mark.django_db
def test_postmark_delivery_webhook_marks_the_booking(booking, client):
    booking.confirmation_email_status = "sent"
    booking.confirmation_email_provider_id = "pm-message-1"
    booking.save(update_fields=["confirmation_email_status", "confirmation_email_provider_id"])

    response = client.post(
        "/api/v1/email/webhooks/postmark/",
        {"RecordType": "Delivery", "MessageID": "pm-message-1", "Recipient": "anna@example.com"},
        content_type="application/json",
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.confirmation_email_status == "delivered"


@pytest.mark.django_db
def test_postmark_complaint_webhook_marks_the_booking(booking, client):
    booking.confirmation_email_status = "delivered"
    booking.confirmation_email_provider_id = "pm-message-2"
    booking.save(update_fields=["confirmation_email_status", "confirmation_email_provider_id"])

    response = client.post(
        "/api/v1/email/webhooks/postmark/",
        {"RecordType": "SpamComplaint", "MessageID": "pm-message-2", "Description": "marked as spam"},
        content_type="application/json",
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.confirmation_email_status == "complained"
    assert booking.confirmation_email_error == "marked as spam"


@pytest.mark.django_db
def test_unknown_provider_message_id_is_ignored(client):
    response = client.post(
        "/api/v1/email/webhooks/postmark/",
        {"RecordType": "Bounce", "MessageID": "does-not-exist"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.django_db
def test_ops_dashboard_shows_email_delivery(booking, client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="reception", password="pw", is_staff=True
    )
    client.force_login(user)

    booking.confirmation_email_status = "failed"
    booking.confirmation_email_attempts = 2
    booking.confirmation_email_error = "smtp rejected the message"
    booking.save(
        update_fields=[
            "confirmation_email_status",
            "confirmation_email_attempts",
            "confirmation_email_error",
        ]
    )

    response = client.get("/ops/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Email delivery" in content
    assert "Email failures" in content
    assert booking.reference in content
    assert "smtp rejected the message" in content
