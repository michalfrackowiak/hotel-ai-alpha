"""Email delivery status and bounce webhooks."""

from __future__ import annotations

import pytest

from apps.hotel.booking_service import confirm_booking, create_booking


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
        phone="+48 600 100 200",
        adults=2,
    )


@pytest.mark.django_db
def test_bounce_webhook_marks_the_booking(booking, client):
    confirm_booking(booking.reference, actor="guest", channel="web")

    response = client.post(
        "/api/v1/email/webhooks/ses/",
        {"reference": booking.reference, "event": "bounced", "reason": "mailbox full"},
        content_type="application/json",
    )

    assert response.status_code == 200
    booking.refresh_from_db()
    assert booking.confirmation_email_status == "bounced"
    assert booking.confirmation_email_error == "mailbox full"


@pytest.mark.django_db
def test_webhook_rejects_a_bad_secret(booking, client, settings):
    settings.EMAIL_WEBHOOK_SECRET = "topsecret"
    confirm_booking(booking.reference, actor="guest", channel="web")

    response = client.post(
        "/api/v1/email/webhooks/ses/",
        {"reference": booking.reference, "event": "delivered"},
        content_type="application/json",
        HTTP_X_EMAIL_WEBHOOK_SECRET="wrong",
    )

    assert response.status_code == 403