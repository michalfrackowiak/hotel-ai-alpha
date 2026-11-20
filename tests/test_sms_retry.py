"""Confirmation SMS retry backoff, scheduler queue and command."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.hotel import notifications
from apps.hotel.booking_service import create_booking
from apps.hotel.models import Booking, SmsDelivery
from apps.hotel.notifications import (
    max_sms_attempts,
    send_booking_confirmation_sms,
    send_pending_confirmation_sms,
    sms_retry_delay_seconds,
)
from apps.hotel.sms_providers import SmsDeliveryResult


class FailingSmsProvider:
    name = "failing"

    def send(self, message):
        return SmsDeliveryResult(sent=False, error="carrier rejected the message")


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


def test_sms_retry_delay_doubles_and_is_capped(settings):
    settings.SMS_RETRY_BASE_SECONDS = 60
    settings.SMS_RETRY_MAX_SECONDS = 200

    assert [sms_retry_delay_seconds(n) for n in (1, 2, 3, 4)] == [60, 120, 200, 200]


@pytest.mark.django_db
def test_failed_sms_send_schedules_a_retry(booking, monkeypatch, settings):
    settings.SMS_RETRY_BASE_SECONDS = 60
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())

    assert send_booking_confirmation_sms(booking) is False

    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "failed"
    assert booking.confirmation_sms_error == "carrier rejected the message"
    assert booking.confirmation_sms_attempts == 1
    assert booking.confirmation_sms_last_attempt_at is not None
    assert booking.confirmation_sms_next_retry_at is not None
    delay = booking.confirmation_sms_next_retry_at - booking.confirmation_sms_last_attempt_at
    assert delay == timedelta(seconds=60)
    delivery = SmsDelivery.objects.get(booking=booking)
    assert delivery.status == SmsDelivery.Status.FAILED
    assert delivery.error_message == "carrier rejected the message"


@pytest.mark.django_db
def test_successful_sms_send_clears_the_retry_schedule(booking):
    assert send_booking_confirmation_sms(booking) is True

    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "sent"
    assert booking.confirmation_sms_attempts == 1
    assert booking.confirmation_sms_last_attempt_at is not None
    assert booking.confirmation_sms_next_retry_at is None
    assert booking.confirmation_sms_error == ""
    assert booking.confirmation_sms_sent_at is not None
    delivery = SmsDelivery.objects.get(booking=booking)
    assert delivery.status == SmsDelivery.Status.SENT


@pytest.mark.django_db
def test_sms_retry_waits_for_the_backoff_window(booking, monkeypatch, settings):
    settings.SMS_RETRY_BASE_SECONDS = 300
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())
    assert send_booking_confirmation_sms(booking) is False

    # Still inside the backoff window: the retry queue must leave it alone.
    assert send_pending_confirmation_sms() == 0

    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_next_retry_at=timezone.now() - timedelta(seconds=1)
    )
    monkeypatch.undo()

    assert send_pending_confirmation_sms() == 1
    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "sent"
    assert booking.confirmation_sms_attempts == 2
    assert booking.confirmation_sms_next_retry_at is None
    assert SmsDelivery.objects.filter(booking=booking).count() == 2


@pytest.mark.django_db
def test_sms_retry_stops_at_the_attempt_cap(booking, monkeypatch, settings):
    settings.SMS_MAX_ATTEMPTS = 2
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())
    assert max_sms_attempts() == 2

    assert send_booking_confirmation_sms(booking) is False
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_next_retry_at=timezone.now() - timedelta(seconds=1)
    )

    send_pending_confirmation_sms()
    booking.refresh_from_db()
    assert booking.confirmation_sms_attempts == 2
    assert booking.confirmation_sms_next_retry_at is None

    send_pending_confirmation_sms()
    booking.refresh_from_db()
    assert booking.confirmation_sms_attempts == 2


@pytest.mark.django_db
def test_send_booking_sms_command_retries_due_messages(booking, monkeypatch):
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())
    assert send_booking_confirmation_sms(booking) is False

    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_next_retry_at=timezone.now() - timedelta(seconds=1)
    )
    monkeypatch.undo()

    call_command("send_booking_sms")

    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "sent"
    assert booking.confirmation_sms_attempts == 2


@pytest.mark.django_db
def test_pending_sms_only_sends_unsent_confirmed_bookings_with_phone(
    booking, room_type, rooms, stay_dates, monkeypatch
):
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())
    assert send_booking_confirmation_sms(booking) is False
    Booking.objects.filter(pk=booking.pk).update(
        confirmation_sms_next_retry_at=timezone.now() - timedelta(seconds=1)
    )

    check_in, check_out = stay_dates
    sent = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Jan",
        last_name="Nowak",
        email="jan@example.com",
        phone="+48 600 100 200",
        adults=1,
    )
    send_booking_confirmation_sms(sent)

    later_check_in = check_in + timedelta(days=30)
    later_check_out = check_out + timedelta(days=30)
    no_phone = create_booking(
        room_type=room_type,
        check_in=later_check_in,
        check_out=later_check_out,
        first_name="Ola",
        last_name="Lis",
        email="ola@example.com",
        adults=1,
    )
    no_phone.client.phone = ""
    no_phone.client.save(update_fields=["phone"])
    no_phone.status = Booking.Status.CONFIRMED
    no_phone.save(update_fields=["status"])

    monkeypatch.undo()
    # pending queue should retry only `booking` (failed, due) and skip delivered/no-phone
    assert send_pending_confirmation_sms() == 1
    booking.refresh_from_db()
    assert booking.confirmation_sms_status == "sent"
    assert SmsDelivery.objects.filter(booking=sent).count() == 1
    assert SmsDelivery.objects.filter(booking=no_phone).count() == 0


@pytest.mark.django_db
def test_force_resend_sms_sends_even_when_already_marked_sent(booking):
    assert send_booking_confirmation_sms(booking) is True
    assert SmsDelivery.objects.filter(booking=booking).count() == 1

    assert send_booking_confirmation_sms(booking, force=True) is True
    assert SmsDelivery.objects.filter(booking=booking).count() == 2
    booking.refresh_from_db()
    assert booking.confirmation_sms_attempts == 2
    assert booking.confirmation_sms_next_retry_at is None


@pytest.mark.django_db
def test_ops_dashboard_shows_sms_retry_queue_and_next_retry(booking, client, monkeypatch, settings):
    from django.contrib.auth import get_user_model

    settings.SMS_RETRY_BASE_SECONDS = 60
    monkeypatch.setattr(notifications, "get_sms_provider", lambda: FailingSmsProvider())
    assert send_booking_confirmation_sms(booking) is False
    booking.refresh_from_db()

    user = get_user_model().objects.create_user(username="reception-sms", password="pw", is_staff=True)
    client.force_login(user)

    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "SMS waiting to retry" in content
    assert "Next retry" in content
    assert booking.reference in content
    assert "carrier rejected the message" in content
    # failed booking is due in the future, so retry queue count is 0 now but problem row shows next retry
    assert booking.confirmation_sms_next_retry_at is not None
    assert str(booking.confirmation_sms_next_retry_at.day) in content or str(
        booking.confirmation_sms_next_retry_at.strftime("%d")
    ) in content
