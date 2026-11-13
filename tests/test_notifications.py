"""Confirmation email delivery, iCalendar attachments and retries."""

from __future__ import annotations

import pytest
from django.core import mail
from django.core.management import call_command

from apps.hotel.booking_service import cancel_booking, confirm_booking, create_booking
from apps.hotel.ics import build_booking_ics
from apps.hotel.models import Booking
from apps.hotel.notifications import (
    send_booking_confirmation,
    send_pending_confirmation_emails,
)


@pytest.fixture
def booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    return create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
        language="en",
    )


@pytest.mark.django_db
def test_confirmation_sends_one_email_with_ics(booking, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["anna@example.com"]
    assert booking.reference in message.subject
    assert "Manage your booking" in message.body
    assert "anna@example.com" not in message.alternatives[0][0]  # no contact leak in the body
    assert any(name.endswith(".ics") for name, _content, _mimetype in message.attachments)

    booking.refresh_from_db()
    assert booking.confirmation_email_sent_at is not None


@pytest.mark.django_db
def test_confirmation_email_is_not_sent_twice(booking, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)
    mail.outbox.clear()
    booking.refresh_from_db()

    assert send_booking_confirmation(booking) is False
    assert mail.outbox == []


@pytest.mark.django_db
def test_force_resend_ignores_the_delivery_marker(booking):
    assert send_booking_confirmation(booking) is True
    assert len(mail.outbox) == 1

    assert send_booking_confirmation(booking, force=True) is True
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_email_is_skipped_without_an_address(booking):
    booking.client.email = ""
    booking.client.save(update_fields=["email"])

    assert send_booking_confirmation(booking) is False
    assert mail.outbox == []


@pytest.mark.django_db
def test_cancellation_does_not_send_a_confirmation(booking, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking.reference)

    assert mail.outbox == []


@pytest.mark.django_db
def test_retry_command_only_sends_undelivered_confirmations(booking, room_type, rooms, stay_dates):
    confirmed = booking
    confirmed.status = Booking.Status.CONFIRMED
    confirmed.save(update_fields=["status"])

    check_in, check_out = stay_dates
    delivered = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Jan",
        last_name="Nowak",
        email="jan@example.com",
        adults=1,
    )
    send_booking_confirmation(delivered)
    mail.outbox.clear()

    assert send_pending_confirmation_emails() == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]

    call_command("send_booking_emails")
    assert len(mail.outbox) == 1


def test_ics_contains_the_stay(booking):
    ics = build_booking_ics(booking)

    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert f"UID:{booking.reference}@" in ics
    assert f"DTSTART;VALUE=DATE:{booking.check_in:%Y%m%d}" in ics
    assert f"DTEND;VALUE=DATE:{booking.check_out:%Y%m%d}" in ics
    assert booking.reference in ics
